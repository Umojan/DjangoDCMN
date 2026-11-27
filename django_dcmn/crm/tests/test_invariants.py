"""DESIGN §2 invariants 1, 2, 4, 5, 9, 10 and 11, and plan §0 rule 1 (no legacy Zoho writer imported).

The end-to-end runs go through the real `quotes` intake and pipeline and the engine, answered by `FakeCrm`
behind the NoNetworkTestCase spy at `requests.Session.request`.
"""

import ast
import copy
import io
import json
import logging
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib import admin
from django.core import mail
from django.core.management import call_command
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, pre_save
from django.test import RequestFactory, override_settings
from django.utils import timezone

from crm import engine, rules, sources
from crm.admin import ServiceLinkAdmin
from crm.intake import start_intake
from crm.models import EngineRequest, ServiceLink, ServiceSignal
from crm.registry import REGISTRY
from crm.tasks import purge_engine_pii, reconcile_service_links
from crm.testing import FakeResponse
from crm.tests.support import APOSTILLE_COPY_ID, COPY_TIME, GAQL_ID, EngineTestCase, make_link, saved_quote
from crm.writeback import write_back
from crm.zoho import KEY_FIELD
from orders.models import PhoneCallLead, QuoteRequest, ZohoSyncJob
from quotes import phone_match
from quotes.intake import RequestMeta, attribution_from_body, save_submission
from quotes.models import GAQL_MODULE, QuoteSubmission
from quotes.tests.test_contract import load_fixture, normalised

APP_ROOT = Path(__file__).resolve().parents[2]
LEGACY_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'legacy_quote_request.json'

# The client of the end-to-end runs; none of these may reach a log record or a stored engine result.
CLIENT_NAME = 'ZZ TEST Ingrid Vasquez-Lund'
CLIENT_EMAIL = 'ingrid.vasquez@example.com'
CLIENT_PHONE = '+1 202 555 0168'
CLIENT_MESSAGE = 'Please call after five, the gate code is 4471'
MANAGER_EMAIL = 'manager.vl@example.com'
PII = (CLIENT_EMAIL, 'ingrid.vasquez', '+12025550168', '2025550168', '555 0168', '555-0168', 'Vasquez', 'Ingrid',
       'gate code', MANAGER_EMAIL)

LEGACY_WRITERS = ('create_attribution_record', 'add_tags_to_record', 'create_record_idempotently',
                  'update_record_fields')
LEGACY_TURNSTILE = ('orders.services.applications', 'verify_turnstile')
# Plan §0 rule 1: what the new code may import from `orders` (ATTRIBUTION_FIELD_MAP is a pure constant).
ALLOWED_ORDERS_IMPORTS = {
    'orders.zoho_sync': {'get_access_token', '_split_contact_name'},
    'orders.zoho_errors': {'ZohoTransientError', 'ZohoPermanentError'},
    'orders.services.attribution': {'clean_attribution_data', 'build_zoho_attribution_payload',
                                    'extract_attribution_from_request', 'ATTRIBUTION_FIELD_MAP'},
    'orders.utils': {'generate_tid'},
    'orders.tasks': {'send_tracking_email_task'},
    'orders.models': {'Track', 'ZohoSyncJob', 'PhoneCallLead', 'QuoteRequest'},
    # DESIGN §10.2: crm/translation_capture.py subclasses the legacy create views (the response is never changed).
    'orders.views.orders': {'CreateFbiOrderView', 'CreateApostilleOrderView', 'CreateEmbassyOrderView',
                            'CreateMarriageOrderView', 'CreateFingerprintingView', 'CreatePreCheckView'},
}


# Invariant 4: the only modules of the new code that make HTTP calls, and what they may call.
HTTP_MODULES = {'crm/zoho.py': 'Zoho CRM (every Zoho call)', 'quotes/antispam.py': 'Cloudflare Turnstile siteverify'}
HTTP_LIBRARIES = ('requests', 'urllib.request', 'http.client', 'httpx', 'urllib3', 'aiohttp')
FROZEN_APPS = ('orders', 'reviews')   # invariant 2 (and plan §0 rule 1 for reviews/)
DEFAULT_BRANCHES = ('main', 'origin/main', 'master', 'origin/master')


def git(*args: str) -> str | None:
    """Output of a local git command run in the Django root; None when git or the repository is missing."""
    if shutil.which('git') is None:
        return None
    result = subprocess.run(['git', *args], cwd=APP_ROOT, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def source_files(*, tests: bool) -> list[Path]:
    files = [path for app in ('crm', 'quotes') for path in sorted((APP_ROOT / app).rglob('*.py'))
             if '/migrations/' not in str(path)]
    return [path for path in files if tests or '/tests/' not in str(path)]


def _root_name(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ''


def legacy_writer_uses(source: str) -> list[str]:
    """Imports and attribute uses of the legacy Zoho writers (and the legacy Turnstile check) in `source`."""
    tree = ast.parse(source)
    found, bound = [], {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] == 'orders':
            for alias in node.names:
                if alias.name in LEGACY_WRITERS or (node.module, alias.name) == LEGACY_TURNSTILE:
                    found.append(f'{node.lineno}: from {node.module} import {alias.name}')
                bound[alias.asname or alias.name] = f'{node.module}.{alias.name}'
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] == 'orders':
                    bound[alias.asname or 'orders'] = alias.name if alias.asname else 'orders'
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or _root_name(node) not in bound:
            continue
        dotted = ast.unparse(node).replace(_root_name(node), bound[_root_name(node)], 1)
        if node.attr in LEGACY_WRITERS or dotted == '.'.join(LEGACY_TURNSTILE):
            found.append(f'{node.lineno}: {dotted}')
    return found


def matched_writes(source: str) -> list[str]:
    """Assignments, keyword arguments and update_fields entries naming a `matched_*` field."""
    found = []
    for node in ast.walk(ast.parse(source)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
            node, (ast.AugAssign, ast.AnnAssign)) else []
        found += [f'{node.lineno}: {ast.unparse(t)} =' for t in targets
                  if isinstance(t, ast.Attribute) and t.attr.startswith('matched_')]
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (keyword.arg or '').startswith('matched_'):
                    found.append(f'{node.lineno}: {keyword.arg}=')
                if keyword.arg == 'update_fields':
                    found += [f'{node.lineno}: update_fields {c.value}' for c in ast.walk(keyword.value)
                              if isinstance(c, ast.Constant) and str(c.value).startswith('matched_')]
    return found


@contextmanager
def captured_logs(*names: str):
    """Every record of the `names` loggers at DEBUG, whatever their configured level ('' is the root logger)."""
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Collect(logging.DEBUG)
    saved = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level, logger.propagate))
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
    try:
        yield records
    finally:
        for logger, level, propagate in saved:
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate


def with_client(body: dict) -> None:
    body['contact'].update(full_name=CLIENT_NAME, email=CLIENT_EMAIL, phone=CLIENT_PHONE, message=CLIENT_MESSAGE)


class EndToEnd(EngineTestCase):
    """The new code end to end: intake, pipeline, auto-intake, buttons, adoption, retries and write-back."""

    def submit(self, *, suspect: bool = False) -> QuoteSubmission:
        """POST-equivalent of a v5 quote: save_submission, then its on-commit pipeline (eager Celery)."""
        body = copy.deepcopy(load_fixture('v5_success')['body'])
        body['submission_id'] = str(uuid.uuid4())
        with_client(body)
        if suspect:
            body.pop('timing')
        meta = RequestMeta(ip='203.0.113.9', user_agent='test', turnstile_result='failed' if suspect else 'skipped',
                           attribution=attribution_from_body(body))
        with self.captureOnCommitCallbacks(execute=True):
            quote = save_submission(normalised(body), meta).quote
        quote.refresh_from_db()
        return quote

    def click(self, source, services: list[str], **kwargs) -> engine.EngineResult:
        if isinstance(source, QuoteSubmission):
            source = sources.for_quote(source)
        values = {'mode': 'create', 'trigger': 'zoho_button', 'caller': 'zoho',
                  'actor': {'kind': 'zoho_user', 'email': MANAGER_EMAIL, 'initials': 'VL'}, **kwargs}
        with self.captureOnCommitCallbacks(execute=True):
            return engine.handle(source, services, **values)

    def run_everything(self) -> dict:
        """Every kind of Zoho write the new code makes, once or more; returns the objects it made."""
        with override_settings(QUOTE_AUTO_INTAKE='primary', E2E_ATTRIBUTION=True, QUOTE_V3_ZOHO_SYNC=True), \
                captured_logs('crm', 'quotes'):   # expected warnings (the failed create) stay off the test output
            return self._run_everything()

    def _run_everything(self) -> dict:
        made = {'quote': self.submit()}                                   # contact, attribution, GAQL, Note, intake
        quote = made['quote']
        self.assertEqual(quote.sync_status, 'synced')
        made['suspect'] = self.submit(suspect=True)                       # GAQL + spam tag + "possible spam" Note
        self.click(quote, ['*'], trigger='create_all')                    # the remaining line (translation)
        self.click(quote, ['embassy'], caller='panel', trigger='panel_add')   # a manager line, queued
        self.crm.fail[('POST', REGISTRY['i9'].module)].append(FakeResponse(400, {'data': [{
            'code': 'DUPLICATE_DATA', 'status': 'error', 'details': {'api_name': KEY_FIELD}}]}))
        self.click(quote, ['i9'])                                         # DUPLICATE_DATA without id → upsert
        self.crm.fail[('POST', 'Embassy_Legalization')].append(FakeResponse(400, {'data': [{
            'code': 'INVALID_DATA', 'status': 'error', 'details': {'api_name': 'Country'}}]}))
        self.click(quote, ['embassy'], trigger='create_again', options={'create_again': True})   # a failure

        adopted = saved_quote('svc_apostille', body_changes=with_client)  # a workflow copy adopted and enriched
        self.gaql_record(adopted, GET_A_QUOTE_LEADS='Apostille Service')
        self.crm.load_timeline(GAQL_MODULE, GAQL_ID)
        spec = REGISTRY['apostille']
        self.crm.add(spec.module, id=APOSTILLE_COPY_ID, Name=spec.copy_label, Created_Time=COPY_TIME,
                     **{spec.stage_field: spec.intake_stage()}, **{name: adopted.email for name in spec.client.emails})
        self.click(adopted, ['apostille'])
        made['adopted'] = adopted

        undecided = saved_quote('svc_apostille', zoho_record_id='6686316000033717099', body_changes=with_client)
        self.gaql_record(undecided)
        self.crm.add(spec.module, Name='Apostille · by hand', Client_Email=undecided.email,
                     Status='Initial Contact Made - SEND BY GMAIL',
                     Created_Time=(timezone.now() - timedelta(days=2)).isoformat())
        self.click(undecided, ['apostille'])                              # needs decision: candidates, a Note
        made['undecided'] = undecided
        for source_key in sorted(set(ServiceLink.objects.values_list('source_key', flat=True))):
            write_back(source_key)

        outcomes = {(link.source_key, link.service_key, link.seq): (link.state, link.origin)
                    for link in ServiceLink.objects.all()}
        self.assertEqual(outcomes, {
            (f'qr:{quote.pk}', 'apostille', 1): ('created', 'created'),       # auto-intake
            (f'qr:{quote.pk}', 'translation', 1): ('created', 'created'),
            (f'qr:{quote.pk}', 'embassy', 1): ('created', 'created'),
            (f'qr:{quote.pk}', 'embassy', 2): ('failed', ''),
            (f'qr:{quote.pk}', 'i9', 1): ('created', 'created'),
            (f'qr:{adopted.pk}', 'apostille', 1): ('adopted', 'adopted_copy'),
            (f'qr:{undecided.pk}', 'apostille', 1): ('needs_decision', ''),
        })
        return made


class InvariantTests(EndToEnd):
    def test_legacy_quote_endpoint_unchanged(self):
        recorded = json.loads(LEGACY_FIXTURE.read_text(encoding='utf-8'))
        flag_sets = {
            'new code off': {'QUOTE_V3_ENABLED': False, 'CRM_ENGINE_MODE': 'off', 'QUOTE_V3_SHADOW_LEGACY': False},
            'new code on': {'QUOTE_V3_ENABLED': True, 'CRM_ENGINE_MODE': 'live', 'QUOTE_AUTO_INTAKE': 'primary',
                            'CRM_ENGINE_TEST_ONLY': False, 'E2E_ATTRIBUTION': True},
            'shadow on (S1)': {'QUOTE_V3_SHADOW_LEGACY': True},
        }
        from quotes import signals   # importing it connects the receiver: start from "not connected"

        post_save.disconnect(sender=QuoteRequest, dispatch_uid=signals.DISPATCH_UID)
        for label, flags in flag_sets.items():
            shadow = flags.get('QUOTE_V3_SHADOW_LEGACY', False)
            if shadow:   # QuotesConfig.ready() connects the receiver at start-up when the flag is on
                post_save.connect(signals.shadow_legacy_quote, sender=QuoteRequest, dispatch_uid=signals.DISPATCH_UID)
            with self.subTest(label), override_settings(**flags):
                QuoteRequest.objects.all().delete()
                mail.outbox.clear()
                bodies = [recorded['request'], recorded['request'],
                          {k: v for k, v in recorded['request'].items() if k != 'email'}]
                with self.captureOnCommitCallbacks() as callbacks:
                    responses = [self.client.post(recorded['path'], body, content_type='application/json')
                                 for body in bodies]

                order = QuoteRequest.objects.get()
                for response, expected in zip(responses, recorded['responses']):
                    self.assertEqual(response.status_code, expected['status'])
                    self.assertEqual(response['Content-Type'], expected['content_type'])
                    self.assertEqual(response.content.decode(),
                                     expected['content'].replace('__ORDER_ID__', str(order.pk)))
                row = {k: v for k, v in QuoteRequest.objects.values().get().items() if k not in ('id', 'created_at')}
                self.assertEqual(row, recorded['record'])
                self.assertEqual(list(ZohoSyncJob.objects.filter(order_id=order.pk).values(
                    'order_type', 'status', 'tracking_id')), recorded['sync_jobs'])
                (email,) = mail.outbox
                today = timezone.now().strftime('%Y-%m-%d')
                staff = recorded['staff_email']
                self.assertEqual(email.subject, staff['subject'].replace('__DATE__', today))
                self.assertEqual(email.body, staff['body'].replace('__ORDER_ID__', str(order.pk)))
                self.assertEqual(email.to, staff['to'])
                self.assertEqual(email.extra_headers, {
                    k: v.replace('__DATE__', today).replace('__ORDER_ID__', str(order.pk))
                    for k, v in staff['headers'].items()})
                # The legacy Zoho sync publish, plus the shadow's own publish (90 s later, GETs only) in S1.
                self.assertEqual(len(callbacks), 2 if shadow else 1)
                self.assertFalse(QuoteSubmission.objects.exists())
                self.assertFalse(EngineRequest.objects.exists())
                self.assertFalse(ServiceLink.objects.exists() or ServiceSignal.objects.exists())
            post_save.disconnect(sender=QuoteRequest, dispatch_uid=signals.DISPATCH_UID)
        self.assertNoHttp()

    def test_orders_and_reviews_unchanged(self):
        """Invariant 2: no file under orders/ (or reviews/) differs from the branch point, committed or not."""
        if git('rev-parse', '--is-inside-work-tree') is None:
            self.skipTest('not a git checkout')
        base = next((out.strip() for ref in DEFAULT_BRANCHES if (out := git('merge-base', 'HEAD', ref))), None)
        if not base:
            self.skipTest('no default branch to compare with')
        changed = git('diff', '--name-only', base, '--', *FROZEN_APPS)
        untracked = git('ls-files', '--others', '--exclude-standard', '--', *FROZEN_APPS)
        self.assertEqual((changed or '').split() + (untracked or '').split(), [],
                         'DESIGN §2 invariant 2: orders/ and reviews/ must not change')

    def test_every_http_call_goes_through_crm_zoho(self):
        """Invariant 4: only crm/zoho.py talks to Zoho; the only other HTTP call is the Turnstile siteverify."""
        users = {}
        for path in source_files(tests=False):
            if path == APP_ROOT / 'crm' / 'testing.py':   # the test base that patches requests.Session.request
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                if any(name == lib or name.startswith(f'{lib}.') for name in names for lib in HTTP_LIBRARIES):
                    users.setdefault(str(path.relative_to(APP_ROOT)), node.lineno)
        self.assertEqual(set(users), set(HTTP_MODULES))

        antispam = (APP_ROOT / 'quotes' / 'antispam.py').read_text(encoding='utf-8')
        calls = [ast.unparse(node.func) for node in ast.walk(ast.parse(antispam))
                 if isinstance(node, ast.Call) and _root_name(node.func) == 'requests']
        self.assertEqual(calls, ['requests.post'])
        self.assertIn("TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'", antispam)

    def test_every_write_has_trigger_and_every_create_has_key(self):
        self.run_everything()

        writes = [c for c in self.http.calls if c.method != 'GET']
        self.assertTrue(writes)
        kinds = set()
        for call in writes:
            with self.subTest(call=f'{call.method} {call.url}'):
                self.assertTrue(call.url.startswith('https://www.zohoapis.com/crm/v2/'), call.url)
                self.assertIn(call.method, ('POST', 'PUT'))
                self.assertEqual(call.json['trigger'], [])
                path = call.url.removeprefix('https://www.zohoapis.com/crm/v2/').split('/')
                if call.method == 'PUT':
                    kinds.add('update')
                elif path[0] == 'Notes':
                    kinds.add('note')
                elif path[2:] == ['actions', 'add_tags']:
                    kinds.add('tags')
                    self.assertEqual(call.params['over_write'], 'false')
                elif path[1:] == ['upsert']:
                    kinds.add('upsert')
                    self.assertEqual(call.json['duplicate_check_fields'], [KEY_FIELD])
                    self.assertTrue(call.json['data'][0][KEY_FIELD].startswith('dcmn:'))
                elif path[0] == 'Contacts':
                    kinds.add('contact')   # keyed by the unique Email (DESIGN §5 step 1), searched first
                    self.assertEqual(call.json['data'][0]['Email'], CLIENT_EMAIL)
                else:
                    self.assertEqual(len(path), 1, call.url)
                    kinds.add(f'create:{path[0]}')
                    (record,) = call.json['data']
                    self.assertRegex(record[KEY_FIELD], r'^dcmn:[a-z]')
        self.assertLessEqual({'update', 'note', 'tags', 'upsert', 'contact', f'create:{GAQL_MODULE}',
                              'create:Lead_Attribution_Records', 'create:Apostille_Services',
                              'create:Translation_Services', 'create:Embassy_Legalization', 'create:I_9_Verification'},
                             kinds)
        self.assertFalse(any(c.url.startswith('https://accounts.zoho.com/') for c in self.http.calls))

    def test_no_legacy_zoho_writer_imported(self):
        caught = {
            'from orders.zoho_sync import create_record_idempotently': 1,
            'from orders.zoho_sync import update_record_fields as write': 1,
            'from orders import zoho_sync\nzoho_sync.add_tags_to_record(1)': 1,
            'import orders.zoho_sync\norders.zoho_sync.create_attribution_record(1)': 1,
            'import orders.zoho_sync as legacy\nlegacy.update_record_fields(1)': 1,
            'from orders.services.applications import verify_turnstile': 1,
            'from orders.services import applications\napplications.verify_turnstile("t")': 1,
            'from .antispam import verify_turnstile\nverify_turnstile("t", None, "s")': 0,
            'from orders.zoho_sync import get_access_token\nget_access_token(force_refresh=True)': 0,
        }
        for sample, expected in caught.items():   # the scanner itself
            self.assertEqual(len(legacy_writer_uses(sample)), expected, sample)

        uses = {str(path.relative_to(APP_ROOT)): found for path in source_files(tests=True)
                if (found := legacy_writer_uses(path.read_text(encoding='utf-8')))}
        self.assertEqual(uses, {})

        outside = []
        for path in source_files(tests=False):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] == 'orders':
                    allowed = ALLOWED_ORDERS_IMPORTS.get(node.module, set())
                    outside += [f'{path.relative_to(APP_ROOT)}:{node.lineno} {node.module}.{alias.name}'
                                for alias in node.names if alias.name not in allowed]
                elif isinstance(node, ast.Import):
                    outside += [f'{path.relative_to(APP_ROOT)}:{node.lineno} import {alias.name}'
                                for alias in node.names if alias.name.split('.')[0] == 'orders']
        self.assertEqual(outside, [])

    def test_no_pii_in_logs(self):
        with captured_logs('', 'crm', 'quotes') as records:   # the root logger: legacy and library loggers too
            made = self.run_everything()
            start_intake(made['suspect'].pk)
            with override_settings(CRM_ENGINE_MODE='off'):
                self.click(made['quote'], ['apostille'])
            purge_engine_pii()
            reconcile_service_links()

        formatter = logging.Formatter('%(name)s %(levelname)s %(message)s')
        lines = [formatter.format(record) for record in records]
        self.assertGreater(len(lines), 20)
        self.assertTrue(any(line.startswith('crm.engine INFO engine_created') for line in lines))
        self.assertTrue(any(line.startswith('crm.engine WARNING engine_failed') for line in lines))
        for line in lines:
            for value in PII:
                self.assertNotIn(value.lower(), line.lower(), line)

    def test_phone_call_lead_never_written(self):
        for path in source_files(tests=False):
            self.assertEqual(matched_writes(path.read_text(encoding='utf-8')), [], path)
        self.assertEqual(len(matched_writes('lead.matched_order_id = 1\nPhoneCallLead.objects.update('
                                            'matched_with_form=True)\nlead.save(update_fields=["matched_order_type"])')),
                         3)

        lead = PhoneCallLead.objects.create(
            whatconverts_lead_id='wc-invariant-1', contact_phone='(202) 555-0168', contact_email=CLIENT_EMAIL,
            zoho_module=GAQL_MODULE, zoho_lead_id='6686316000055500011', zoho_synced=True, call_duration=95,
            landing_url='https://www.dcmobilenotary.com/apostille', raw_webhook_data={})
        PhoneCallLead.objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(hours=2))
        before = PhoneCallLead.objects.values().get(pk=lead.pk)

        saves, updates = [], []
        original_update, original_bulk = QuerySet.update, QuerySet.bulk_update

        def spy_update(queryset, **kwargs):
            if queryset.model is PhoneCallLead:
                updates.append(kwargs)
            return original_update(queryset, **kwargs)

        def spy_bulk(queryset, objs, fields, *args, **kwargs):
            if queryset.model is PhoneCallLead:
                updates.append(fields)
            return original_bulk(queryset, objs, fields, *args, **kwargs)

        def on_save(sender, instance, **kwargs):
            saves.append(instance.pk)

        pre_save.connect(on_save, sender=PhoneCallLead, dispatch_uid='invariant_11')
        self.addCleanup(pre_save.disconnect, sender=PhoneCallLead, dispatch_uid='invariant_11')
        with patch.object(QuerySet, 'update', spy_update), patch.object(QuerySet, 'bulk_update', spy_bulk):
            made = self.run_everything()
            phone_match.match_before(made['quote'])
            phone_match.link_recent()

        self.assertTrue(made['quote'].phone_matches.exists())   # the match was found and stored on our side
        self.assertEqual((saves, updates), ([], []))
        self.assertEqual(PhoneCallLead.objects.values().get(pk=lead.pk), before)

    def test_engine_request_for_every_call(self):
        quote = saved_quote(body_changes=with_client)
        self.gaql_record(quote)
        degraded = make_link(saved_quote(), 'apostille', state='degraded', last_error_code='schema_drift')
        failed = make_link(saved_quote(), 'apostille', state='failed', last_error_code='INVALID_DATA')
        retry = make_link(saved_quote(), 'apostille', state='needs_decision')
        signal = ServiceSignal.objects.create(
            dedupe_key=f'apostille_implied:qr:{quote.pk}:apostille', kind='apostille_implied', source_kind='quote',
            source_key=f'qr:{quote.pk}', source_module=GAQL_MODULE, source_record_id=quote.zoho_record_id,
            quote=quote, service_key='apostille')
        staff = RequestFactory().post('/admin/')
        staff.user = type('Staff', (), {'pk': 7})()
        link_admin = ServiceLinkAdmin(ServiceLink, admin.site)

        def committed(call):
            def run():
                with self.captureOnCommitCallbacks(execute=True):
                    call()
            return run

        calls = {   # (caller, what) → a call that makes exactly one EngineRequest
            ('zoho', 'create'): lambda: self.click(quote, ['apostille']),
            ('zoho', 'dry_run'): lambda: self.click(quote, ['translation']),
            ('panel', 'preview'): lambda: self.click(quote, ['embassy'], mode='preview', caller='panel'),
            ('panel', 'queued'): lambda: engine.handle(sources.for_quote(quote), ['embassy'], mode='create',
                                                       trigger='panel_add', caller='panel', actor={},
                                                       client_request_id='click-1'),
            ('panel', 'duplicate'): lambda: engine.handle(sources.for_quote(quote), ['embassy'], mode='create',
                                                          trigger='panel_add', caller='panel', actor={},
                                                          client_request_id='click-1'),
            ('staff', 'bad_mode'): lambda: self.click(quote, ['i9'], caller='staff', mode='delete'),
            ('zoho', 'disabled'): lambda: self.click(quote, ['i9']),
            ('rules', 'suggest'): committed(lambda: rules.evaluate(signal)),
            ('system', 'intake'): committed(lambda: start_intake(saved_quote(intake_mode='primary').pk)),
            ('system', 'rearm_degraded'): committed(reconcile_service_links),
            ('admin', 'crm_reconcile'): committed(lambda: call_command('crm_reconcile', stdout=io.StringIO())),
            ('admin', 'admin_retry'): committed(lambda: link_admin.retry(staff,
                                                                         ServiceLink.objects.filter(pk=retry.pk))),
        }
        modes = {'dry_run': 'dry_run', 'disabled': 'off'}
        with patch.object(ServiceLinkAdmin, 'message_user'):
            for (caller, what), call in calls.items():
                with self.subTest(caller=caller, what=what):
                    before = EngineRequest.objects.count()
                    with override_settings(CRM_ENGINE_MODE=modes.get(what, 'live')):
                        call()
                    self.assertEqual(EngineRequest.objects.count(), before + 1)
                    request = EngineRequest.objects.order_by('-pk').first()
                    self.assertEqual(request.caller, caller)
                    self.assertNotEqual(request.state, 'accepted')
                    if what in ('dry_run', 'duplicate', 'bad_mode', 'disabled'):
                        self.assertEqual(request.error_code or request.state,
                                         {'dry_run': 'done', 'duplicate': 'duplicate_request'}.get(what, what))
        degraded.refresh_from_db()
        failed.refresh_from_db()
        self.assertEqual((degraded.request.trigger, failed.request.trigger), ('rearm_degraded', 'crm_reconcile'))

    def test_stored_engine_result_redacted(self):
        quote = saved_quote(body_changes=with_client)
        self.gaql_record(quote)
        self.crm.add('Apostille_Services', Name=f'Apostille · {CLIENT_NAME}', Client_Email=quote.email,
                     Status='Initial Contact Made - SEND BY GMAIL',
                     Created_Time=(timezone.now() - timedelta(days=2)).isoformat())

        preview = self.click(quote, ['translation'], mode='preview', caller='panel')
        undecided = self.click(quote, ['apostille'])
        with override_settings(CRM_ENGINE_MODE='dry_run'):
            dry_run = self.click(quote, ['embassy'])

        (shown,) = preview.body['results']
        self.assertIn(CLIENT_NAME, json.dumps(shown['payload'], ensure_ascii=False))   # the caller sees it all
        self.assertEqual(undecided.body['results'][0]['status'], 'needs_decision')
        for result in (preview, undecided, dry_run):
            stored = EngineRequest.objects.get(pk=result.request.pk).result
            text = json.dumps(stored, ensure_ascii=False)
            for value in (*PII, CLIENT_MESSAGE, quote.reference):
                self.assertNotIn(value.lower(), text.lower())
            for item in stored['results']:
                if 'payload' in item:
                    self.assertRegex(item['payload_sha256'], r'^[0-9a-f]{64}$')
                    self.assertTrue(all(set(shape) == {'type', 'len'} for shape in item['payload'].values()))
                for candidate in item.get('candidates', []):
                    self.assertLessEqual(set(candidate), {'module', 'id', 'match'})
        stored = EngineRequest.objects.get(pk=undecided.request.pk).result
        self.assertTrue(stored['results'][0]['candidates'])
        self.assertEqual(stored['results'][0]['candidates'][0]['match'], 'same_client')
