"""Ops tasks of the engine (DESIGN §4.5, §7.8 step 7, §12.2): the 90-day PII purge, the Zoho token pre-warm,
the daily digest and the beat heartbeat check."""

import time as clock
import uuid
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm.alerts import BEAT_CHECK_KEY, BEAT_FIRST_CHECK_KEY, BEAT_HEARTBEAT_KEY, BEAT_STALE_AFTER_S
from crm.models import EngineRequest, ServiceLink, ServiceSignal
from crm.tasks import beat_heartbeat, ops_daily_digest, prewarm_zoho_token, purge_engine_pii
from crm.tasks.links import ORPHAN_CACHE_KEY
from crm.tasks.ops import TOKEN_LOCK_KEY
from crm.testing import NoNetworkTestCase, make_quote_submission
from crm.translation_capture import IGNORED_KEY
from quotes.models import QuoteLine, QuotePhoneMatch, QuoteRejection, QuoteSubmission
from quotes.render import ET
from quotes.tasks.emails import send_quote_staff_email

OLD = timedelta(days=91)


class PurgeTests(NoNetworkTestCase):
    def link(self, key: str, state: str, age: timedelta) -> ServiceLink:
        link = ServiceLink.objects.create(
            source_kind='quote', source_key='qr:1', service_key=key, external_key=f'dcmn:qr:1:{key}', state=state,
            actor={'kind': 'zoho_user', 'email': 'manager@example.com', 'initials': 'VL'},
            candidates=[{'module': 'Apostille_Services', 'id': '1', 'name': 'Apostille · Jane Doe', 'match': 'same_client'}])
        ServiceLink.objects.filter(pk=link.pk).update(updated_at=timezone.now() - age)
        return link

    def test_engine_pii_purged_after_90_days(self):
        old_request = EngineRequest.objects.create(caller='zoho', mode='create', ip='203.0.113.7',
                                                   actor={'email': 'manager@example.com', 'initials': 'VL'},
                                                   result={'results': [{'service': 'apostille'}]})
        new_request = EngineRequest.objects.create(caller='zoho', mode='create', ip='203.0.113.8',
                                                   actor={'email': 'manager@example.com'}, result={'ok': True})
        EngineRequest.objects.filter(pk=old_request.pk).update(created_at=timezone.now() - OLD)
        done = self.link('apostille', 'created', OLD)
        running = self.link('embassy', 'pending', OLD)
        recent = self.link('translation', 'failed', timedelta(days=10))
        old_signal = ServiceSignal.objects.create(dedupe_key='translation_requested:form:apostille:1:translation',
                                                  kind='translation_requested', source_kind='form',
                                                  source_key='form:apostille:1', service_key='translation',
                                                  state='evaluated', data={'lang_from': 'Spanish', 'lang_from_raw': 'espanol'})
        waiting = ServiceSignal.objects.create(dedupe_key='translation_requested:form:apostille:2:translation',
                                               kind='translation_requested', source_kind='form',
                                               source_key='form:apostille:2', service_key='translation',
                                               state='waiting_source', data={'lang_from_raw': 'espanol'})
        errored = ServiceSignal.objects.create(dedupe_key='translation_requested:form:fbi:3:translation',
                                               kind='translation_requested', source_kind='form',
                                               source_key='form:fbi:3', service_key='translation',
                                               state='error', data={'lang_to_raw': 'ingles'})
        ServiceSignal.objects.filter(pk__in=[old_signal.pk, waiting.pk, errored.pk]).update(
            created_at=timezone.now() - OLD)

        self.assertEqual(purge_engine_pii(), 'requests=1 links=1 signals=2')

        old_request.refresh_from_db()
        self.assertEqual((old_request.result, old_request.ip), (None, None))
        self.assertEqual(old_request.actor, {'email': '', 'initials': 'VL'})
        new_request.refresh_from_db()
        self.assertEqual((new_request.ip, new_request.actor['email']), ('203.0.113.8', 'manager@example.com'))
        done.refresh_from_db()
        self.assertEqual(done.candidates[0]['name'], '')
        self.assertEqual((done.candidates[0]['id'], done.actor['email'], done.actor['initials']), ('1', '', 'VL'))
        for untouched in (running, recent):
            untouched.refresh_from_db()
            self.assertEqual(untouched.candidates[0]['name'], 'Apostille · Jane Doe')
        old_signal.refresh_from_db()
        self.assertEqual(old_signal.data, {'lang_from': 'Spanish'})
        waiting.refresh_from_db()
        self.assertEqual(waiting.data, {'lang_from_raw': 'espanol'})   # still needed by its rule
        errored.refresh_from_db()
        self.assertEqual(errored.data, {})                             # a deleted source record: terminal

        self.assertEqual(purge_engine_pii(), 'requests=0 links=0 signals=0')   # idempotent
        self.assertNoHttp()


class PrewarmTests(NoNetworkTestCase):
    @patch('crm.tasks.ops.get_access_token', return_value='fresh-token')
    def test_prewarm_under_lock_and_flag_gated(self, get_token):
        with override_settings(CRM_ENGINE_MODE='off', QUOTE_V3_ENABLED=False):
            self.assertEqual(prewarm_zoho_token(), 'skipped')
        get_token.assert_not_called()

        for flags in ({'CRM_ENGINE_MODE': 'live', 'QUOTE_V3_ENABLED': False},
                      {'CRM_ENGINE_MODE': 'off', 'QUOTE_V3_ENABLED': True}):
            with self.subTest(**flags), override_settings(**flags):
                cache.delete(TOKEN_LOCK_KEY)
                get_token.reset_mock()
                self.assertEqual(prewarm_zoho_token(), 'refreshed')
                get_token.assert_called_once_with(force_refresh=True)
                self.assertEqual(prewarm_zoho_token(), 'locked')   # a second worker within 60 s
                get_token.assert_called_once()
        self.assertNoHttp()


def yesterday_noon():
    day = (timezone.now().astimezone(ET) - timedelta(days=1)).date()
    return day, datetime.combine(day, time(12), tzinfo=ET)


@override_settings(OPS_ALERT_EMAILS=['ops@example.com'], BASE_URL='https://api.example.test', CRM_ENGINE_MODE='live')
class DigestTests(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.day, noon = yesterday_noon()
        self.quote = make_quote_submission(full_name='Maria Fernández', email='maria.private@example.com',
                                           phone_raw='(202) 555-0147', is_test=False, sync_status='failed',
                                           sync_step='contact', spam_status='clean')
        self.test_quote = make_quote_submission(sync_status='synced')
        QuoteSubmission.objects.update(created_at=noon)
        QuoteLine.objects.create(quote=self.quote, position=1, service_key='apostille', source='user',
                                 price_flag='unapproved')
        QuoteRejection.objects.create(reason='honeypot')
        QuoteRejection.objects.update(created_at=noon)
        self.failed = ServiceLink.objects.create(
            source_kind='quote', source_key=f'qr:{self.quote.pk}', quote=self.quote, service_key='embassy',
            external_key=f'dcmn:qr:{self.quote.pk}:embassy', state='failed', last_error_code='INVALID_DATA',
            actor={'email': 'manager@example.com'}, candidates=[{'name': 'Maria Fernández'}])
        self.created = ServiceLink.objects.create(
            source_kind='quote', source_key=f'qr:{self.quote.pk}', quote=self.quote, service_key='apostille',
            external_key=f'dcmn:qr:{self.quote.pk}:apostille', state='created', trigger='zoho_button',
            done_at=noon)
        self.deleted = ServiceLink.objects.create(
            source_kind='quote', source_key=f'qr:{self.quote.pk}', quote=self.quote, service_key='translation',
            external_key=f'dcmn:qr:{self.quote.pk}:translation', state='target_deleted', tracking_id='7K2Q9XA3MZ')
        ServiceLink.objects.filter(pk=self.failed.pk).update(updated_at=timezone.now() - timedelta(hours=30))
        EngineRequest.objects.create(caller='zoho', mode='create', state='done', actor={'email': 'x@example.com'})
        EngineRequest.objects.update(created_at=noon)
        cache.set(ORPHAN_CACHE_KEY, [{'quote': self.quote.reference, 'module': 'Apostille_Services',
                                      'id': '6686316000033736004', 'url': 'https://crm.zoho.com/crm/x'}], 3600)

    def digest(self) -> str:
        self.assertEqual(ops_daily_digest(), 'sent')
        (message,) = mail.outbox
        self.assertIn('Daily digest', message.subject)
        return message.body

    def test_digest_lists_open_items_without_pii(self):
        body = self.digest()

        self.assertIn('failed: 1', body)                                  # submissions by status
        self.assertIn('test (ZZ TEST): 1', body)
        self.assertIn('honeypot: 1', body)                                # rejections by reason
        self.assertIn('zoho · done: 1', body)                             # engine requests by result
        self.assertIn('quote · button: 1', body)                          # engine creates per source
        self.assertIn(f'embassy failed · qr:{self.quote.pk} · INVALID_DATA · '
                      f'https://api.example.test/admin/crm/servicelink/{self.failed.pk}/change/', body)
        self.assertIn(f'{self.quote.reference} · contact · '
                      f'https://api.example.test/admin/quotes/quotesubmission/{self.quote.pk}/change/', body)
        self.assertIn(f'{self.quote.reference} · Apostille_Services 6686316000033736004', body)   # orphan copy
        self.assertIn(f'{self.quote.reference} · apostille · ', body)     # unapproved price
        self.assertIn('7K2Q9XA3MZ · translation', body)                   # Track row of a deleted record
        for private in ('Maria', 'maria.private@example.com', '555-0147', 'manager@example.com', 'x@example.com',
                        self.test_quote.full_name, self.test_quote.email):
            self.assertNotIn(private, body)
        self.assertEqual(ops_daily_digest(), 'not_sent')   # once per day
        self.assertNoHttp()

    def test_digest_has_followup_calls_and_capture_ignored(self):
        _, noon = yesterday_noon()
        QuotePhoneMatch.objects.create(quote=self.quote, phone_lead_id=991, direction='call_after_quote',
                                       match_on='phone', phone_record_module='Get_A_Quote_Leads',
                                       phone_record_id='6686316000088800001')
        QuotePhoneMatch.objects.create(quote=self.quote, phone_lead_id=992, direction='call_after_quote',
                                       match_on='phone')                 # no phone record: not counted
        QuotePhoneMatch.objects.update(created_at=noon)
        cache.set(IGNORED_KEY.format(day=self.day.isoformat(), order_type='pre-check'), 2, 3600)
        cache.set(f'crm:auth:body_token_ignored:{self.day.isoformat()}', 3, 3600)

        body = self.digest()

        self.assertIn('followup_call_record\n- follow-up calls that created a phone record: 1', body)
        self.assertIn('translation_capture_ignored\n- pre-check: 2', body)
        self.assertIn('body_token_ignored\n- engine API calls with a key in the body or query: 3', body)


ACTION_KEYS = 'zoho:zk-heartbeat-6f1c2b9e3d4a4e8b9a512c7d0e5f'


@override_settings(OPS_ALERT_EMAILS=['ops@example.com'], CRM_ACTION_KEYS=ACTION_KEYS, QUOTE_V3_ENABLED=True)
class HeartbeatTests(NoNetworkTestCase):
    def stale_alerts(self) -> list:
        return [m for m in mail.outbox if 'Celery beat looks stopped' in m.subject]

    def test_stale_beat_alert_from_config_endpoint(self):
        config = reverse('quote_requests_config')

        self.client.get(config)   # no heartbeat yet: the grace period starts, no email
        self.assertEqual(self.stale_alerts(), [])
        cache.set(BEAT_FIRST_CHECK_KEY, clock.time() - BEAT_STALE_AFTER_S - 60, None)   # missing for 16 min
        cache.delete(BEAT_CHECK_KEY)
        self.client.get(config)
        self.client.get(config)   # checked at most once per 5 min

        (alert,) = self.stale_alerts()
        self.assertIn('no heartbeat recorded', alert.body)
        mail.outbox.clear()
        cache.delete(BEAT_CHECK_KEY)
        cache.delete('alert:reconciler_stale:beat')
        beat_heartbeat()
        self.client.get(config)
        self.assertEqual(self.stale_alerts(), [])   # a fresh heartbeat: no alert
        self.assertNoHttp()

    def test_beat_check_from_engine_api_and_staff_email(self):
        cache.set(BEAT_HEARTBEAT_KEY, clock.time() - 20 * 60, 3600)   # beat stopped 20 minutes ago

        response = self.client.get(reverse('crm_job', args=[uuid.uuid4()]), HTTP_X_DCMN_KEY=ACTION_KEYS[5:])

        self.assertEqual(response.status_code, 404)
        (alert,) = self.stale_alerts()
        self.assertRegex(alert.body, r'last heartbeat \d+ s ago')
        mail.outbox.clear()
        cache.delete(BEAT_CHECK_KEY)
        cache.delete('alert:reconciler_stale:beat')
        self.assertEqual(send_quote_staff_email(0), 'missing')
        self.assertEqual(len(self.stale_alerts()), 1)
        self.assertNoHttp()
