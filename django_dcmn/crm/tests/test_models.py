"""crm and quotes model constraints, legacy-safe foreign keys, flag defaults, task registry and beat schedule (WP1)."""

import ast
import json
from pathlib import Path
from unittest.mock import patch

from celery.signals import worker_ready
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test import override_settings
from django.utils import timezone

from crm.alerts import BEAT_HEARTBEAT_KEY
from crm.models import AutomationRule, ServiceLink, ServiceSignal
from crm.testing import NoNetworkTestCase, make_quote_submission
from django_dcmn.celery import app
from orders.models import PhoneCallLead
from quotes.models import QuotePhoneMatch

NEW_BEAT_TASKS = {
    'quotes.tasks.reconcile_quote_submissions',
    'quotes.tasks.link_recent_phone_leads',
    'quotes.tasks.purge_quote_pii',
    'crm.tasks.reconcile_service_links',
    'crm.tasks.scan_orphan_copies',
    'crm.tasks.refresh_zoho_schema',
    'crm.tasks.apply_translation_signals',
    'crm.tasks.ops_daily_digest',
    'crm.tasks.beat_heartbeat',
    'crm.tasks.prewarm_zoho_token',
    'crm.tasks.purge_engine_pii',
}
LEGACY_BEAT_TASKS = {'orders.tasks.reconcile_pending_zoho_syncs', 'reviews.tasks.send_review_reminders'}
# Tasks queued by code, not by beat, with the arguments of a call.
QUEUED_TASKS = {
    'quotes.tasks.process_quote_submission': (1,),
    'quotes.tasks.send_quote_staff_email': (1,),
    'quotes.tasks.send_quote_confirmation_email': (1,),
    'quotes.tasks.shadow_map_legacy_quote': (1,),
    'crm.tasks.run_service_link': (1,),
    'crm.tasks.writeback_source': ('qr:1',),
}
# DESIGN §13.1: the default of every flag in settings.py, whatever the environment sets.
FLAG_DEFAULTS = {
    'QUOTE_V3_ENABLED': False,
    'QUOTE_V3_TEST_ONLY': True,
    'QUOTE_V3_ZOHO_SYNC': True,
    'QUOTE_AUTO_INTAKE': 'none',
    'INTAKE_CANDIDATE_DAYS': 30,
    'QUOTE_V3_SUBFORM': 'off',
    'QUOTE_V3_CONFIRMATION_EMAIL': False,
    'QUOTE_V3_TEST_EMAILS': False,
    'QUOTE_REPLY_TO': 'support@dcmobilenotary.com',
    'QUOTE_V3_UPLOADS_ENABLED': False,
    'QUOTE_TURNSTILE_REQUIRED': False,
    'QUOTES_HOURLY_CEILING': 60,
    'QUOTES_PROXY_HOPS': 1,
    'QUOTE_V3_SHADOW_LEGACY': False,
    'CRM_ENGINE_MODE': 'off',
    'CRM_ENGINE_TEST_ONLY': True,
    'CRM_SOURCES_ALLOWED': ['Get_A_Quote_Leads'],
    'CRM_ACTION_KEYS': '',
    'CRM_CREATE_LIMIT_PER_SOURCE_HOUR': 10,
    'CRM_CREATE_LIMIT_GLOBAL_HOUR': 60,
    'CRM_PANEL_LINK_ALERT_HOUR': 30,
    'ENGINE_INLINE': True,
    'ENGINE_TID_POLICY': {'Apostille_Services': 'mint_silent', 'Translation_Services': 'mint_silent',
                          'Embassy_Legalization': 'mint_silent', 'Deals': 'mint_silent'},
    'ENGINE_TID_CREATED_EMAIL': 'never',
    'ENGINE_TRIGGER_POLICY': {'*': []},
    'ENGINE_PARITY_CONSTANTS': True,
    'ENGINE_ADOPTION': 'auto_exact',
    'ENGINE_ORPHAN_SCAN': True,
    'ENGINE_COPY_FILES': False,
    'APOSTILLE_FBI_STAGE': '',
    'CRM_RULES_MAX_MODE': 'suggest',
    'TRANSLATION_CAPTURE_ENABLED': False,
    'TRANSLATION_CAPTURE_TYPES': ['fbi', 'apostille', 'embassy', 'marriage', 'fingerprinting'],
    'ZOHO_OWNER_BY_SERVICE': {},
    'ZOHO_QUOTE_OWNER_ID': '',
    'ZOHO_ORG_ID': 'org882204370',
    'ZOHO_SCHEMA_APPLY_ALLOWED': False,
    'OPS_ALERT_EMAILS': [],
    'QUOTES_NOTIFY_EMAILS': [],
    'QUOTES_TEST_NOTIFY_EMAILS': [],
    'PII_HASH_SALT': '',
}
CASTS = {'bool': bool, 'int': int, 'json.loads': json.loads}
FLAGS_OFF = {
    'QUOTE_V3_ENABLED': False,
    'QUOTE_V3_SHADOW_LEGACY': False,
    'QUOTE_V3_CONFIRMATION_EMAIL': False,
    'QUOTE_AUTO_INTAKE': 'none',
    'CRM_ENGINE_MODE': 'off',
    'ENGINE_ORPHAN_SCAN': False,
    'TRANSLATION_CAPTURE_ENABLED': False,
}


class PhoneMatchTests(NoNetworkTestCase):
    def test_phone_lead_delete_not_blocked(self):
        field = QuotePhoneMatch._meta.get_field('phone_lead')
        self.assertIs(field.remote_field.on_delete, models.DO_NOTHING)
        self.assertFalse(field.db_constraint)
        self.assertEqual(field.remote_field.related_name, '+')

        lead = PhoneCallLead.objects.create(whatconverts_lead_id='wc-1', contact_phone='2025550123',
                                            raw_webhook_data={})
        match = QuotePhoneMatch.objects.create(
            quote=make_quote_submission(), phone_lead=lead, whatconverts_lead_id='wc-1',
            direction='call_before_quote', match_on='phone',
        )
        lead_id = lead.pk

        lead.delete()

        self.assertFalse(PhoneCallLead.objects.filter(pk=lead_id).exists())
        match.refresh_from_db()
        self.assertEqual((match.phone_lead_id, match.whatconverts_lead_id), (lead_id, 'wc-1'))
        self.assertIsNone(PhoneCallLead.objects.filter(pk=match.phone_lead_id).first())


class ServiceLinkConstraintTests(NoNetworkTestCase):
    def make_link(self, **fields) -> ServiceLink:
        values = {
            'source_kind': 'quote', 'source_key': 'qr:57', 'service_key': 'apostille', 'seq': 1,
            'external_key': 'dcmn:qr:57:apostille',
        }
        values.update(fields)
        return ServiceLink.objects.create(**values)

    def test_unique_source_service_seq(self):
        self.make_link()

        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_link(external_key='dcmn:qr:57:apostille:other')

        self.make_link(seq=2, external_key='dcmn:qr:57:apostille#2')
        self.make_link(service_key='translation', external_key='dcmn:qr:57:translation')
        self.assertEqual(ServiceLink.objects.count(), 3)

    def test_unique_external_key(self):
        self.make_link()

        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_link(source_key='qr:58')

        self.assertEqual(ServiceLink.objects.count(), 1)

    def test_signal_dedupe_key_is_unique(self):
        values = {'dedupe_key': 'translation_requested:qr:57:translation', 'kind': 'translation_requested',
                  'source_kind': 'quote', 'source_key': 'qr:57', 'service_key': 'translation'}
        ServiceSignal.objects.create(**values)

        with transaction.atomic(), self.assertRaises(IntegrityError):
            ServiceSignal.objects.create(**values)

    def test_create_rule_needs_a_recorded_approval(self):
        rule = AutomationRule(key='apostille_implies_translation', mode='create')
        with self.assertRaises(ValidationError):
            rule.full_clean()

        rule.approved_by = 'vlad'
        rule.approved_at = timezone.now()
        rule.full_clean()


def settings_defaults(names) -> dict:
    """`NAME = config('NAME', default=…, cast=…)` and `_env_list(…)` defaults, read from settings.py itself."""
    tree = ast.parse((Path(settings.BASE_DIR) / 'django_dcmn' / 'settings.py').read_text())
    defaults = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in names and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id in ('config', '_env_list')):
            continue
        keywords = {k.arg: k.value for k in node.value.keywords}
        default = ast.literal_eval(keywords['default']) if 'default' in keywords else ''
        if node.value.func.id == '_env_list':
            default = [v.strip() for v in default.split(',') if v.strip()]
        elif 'cast' in keywords:
            default = CASTS.get(ast.unparse(keywords['cast']), lambda value: value)(default)
        defaults[node.targets[0].id] = default
    return defaults


class FlagDefaultTests(NoNetworkTestCase):
    def test_settings_defaults_are_the_safe_ones(self):
        defaults = settings_defaults(FLAG_DEFAULTS)

        for name, expected in FLAG_DEFAULTS.items():
            with self.subTest(flag=name):
                self.assertIn(name, defaults)
                self.assertEqual(defaults[name], expected)


@override_settings(**FLAGS_OFF)
class BeatTaskTests(NoNetworkTestCase):
    def test_every_beat_task_is_registered_and_noop_when_flags_off(self):
        scheduled = [entry['task'] for entry in settings.CELERY_BEAT_SCHEDULE.values()]
        self.assertEqual(len(scheduled), len(set(scheduled)), 'a task is scheduled twice')
        self.assertEqual(set(scheduled), NEW_BEAT_TASKS | LEGACY_BEAT_TASKS)

        app.autodiscover_tasks(force=True)  # what a worker does at start-up
        registered = set(app.tasks)
        for name in NEW_BEAT_TASKS | set(QUEUED_TASKS):
            self.assertIn(name, registered)

        for name in sorted(NEW_BEAT_TASKS):
            with self.subTest(task=name):
                result = app.tasks[name].apply()
                self.assertTrue(result.successful(), result.traceback)
        for name, args in sorted(QUEUED_TASKS.items()):
            with self.subTest(task=name):
                result = app.tasks[name].apply(args=args)
                self.assertTrue(result.successful(), result.traceback)

        with patch('crm.tasks.refresh_zoho_schema') as refresh:
            worker_ready.send(sender=None)
        refresh.delay.assert_not_called()

        self.assertNoHttp()
        self.assertEqual(mail.outbox, [])   # nor an email (the digest waits for a feature: legacy-rollout-1)
        self.assertIsNotNone(cache.get(BEAT_HEARTBEAT_KEY))  # the one live beat task

    def test_worker_ready_refreshes_schema_once_a_feature_is_on(self):
        with override_settings(CRM_ENGINE_MODE='dry_run'), patch('crm.tasks.refresh_zoho_schema') as refresh:
            worker_ready.send(sender=None)

        refresh.delay.assert_called_once_with()
        self.assertNoHttp()
