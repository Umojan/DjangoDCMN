"""Step runner, retries, lease, circuit, pause, publish fallback, reconciler and trigger policy (WP4)."""

import importlib
import itertools
import sys
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from kombu.exceptions import OperationalError

import crm
from crm import schema, zoho
from crm.schema_spec import GAQL
from crm.testing import FakeResponse, HttpCall, NetworkBlocked, NoNetworkTestCase, make_quote_submission
from crm.tests.test_schema_command import FakeZoho
from crm.tests.test_zoho import item_error, success
from quotes import gaql, pipeline
from quotes.intake import RequestMeta, attribution_from_body, save_submission
from quotes.models import QuoteSubmission
from quotes.tasks import process_quote_submission, send_quote_confirmation_email, send_quote_staff_email
from quotes.tests.test_contract import make_body, normalised

API = 'https://www.zohoapis.com/crm/'
GAQL_TITLE = 'full request'
CREATE_PATHS = (GAQL, 'Lead_Attribution_Records')
WP5_INTAKE = Path(crm.__file__).resolve().parent / 'intake.py'


# --- Shared fixtures of the WP4 tests --------------------------------------------------------------------

def seed_gaql_schema(*, web_quote_request: bool = True) -> None:
    """GAQL metadata after ZOHO-SETUP Z3 (every API field) and, by default, Z4 (`Web Quote Request`)."""
    fake = FakeZoho()
    fake.create_api_fields()
    if web_quote_request:
        fake.add_option(GAQL, 'GET_A_QUOTE_LEADS', gaql.WEB_QUOTE_REQUEST)
    cache.set(schema.CACHE_KEY.format(module=GAQL), schema.by_api_name(schema.normalise(fake.fields[GAQL])),
              schema.CACHE_TTL_S)


def v5_quote(body: dict | None = None, **fields) -> QuoteSubmission:
    """The v5 `success` preset saved as the endpoint saves it (no task runs: on_commit is not executed)."""
    body = body or make_body()
    meta = RequestMeta(ip='203.0.113.7', user_agent='Mozilla/5.0 (test)', turnstile_result='passed',
                       attribution=attribution_from_body(body))
    quote = save_submission(normalised(body), meta).quote
    if fields:
        QuoteSubmission.objects.filter(pk=quote.pk).update(**fields)
        quote.refresh_from_db()
    return quote


class FakeCrm:
    """In-memory Zoho for a run: contacts, creates, tags, Notes and record GETs.

    `fail[(method, path)]` holds responses returned first for that route (an int repeats forever).
    """

    def __init__(self):
        self._ids = itertools.count(6686316000090000001)
        self.contacts: list[dict] = []
        self.notes: dict[str, list[dict]] = defaultdict(list)
        self.records: dict[tuple[str, str], dict] = {}
        self.timelines: dict[str, dict] = {}
        self.fail: dict[tuple[str, str], deque | FakeResponse] = {}

    def new_id(self) -> str:
        return str(next(self._ids))

    def __call__(self, call: HttpCall) -> FakeResponse:
        version, _, path = call.url.removeprefix(API).partition('/')
        route = (call.method, path)
        failure = self.fail.get(route)
        if isinstance(failure, FakeResponse):
            return failure
        if failure:
            return failure.popleft()
        parts = path.split('/')
        if version == 'v6' and parts[-1] == '__timeline':
            return FakeResponse(200, self.timelines.get(parts[1], {'__timeline': []}))
        if route == ('GET', 'Contacts/search'):
            return FakeResponse(200, {'data': self.contacts}) if self.contacts else FakeResponse(204)
        if call.method == 'GET' and parts[-1] == 'search':
            return FakeResponse(204)
        if call.method == 'GET' and len(parts) == 3 and parts[2] == 'Notes':
            notes = self.notes.get(parts[1])
            return FakeResponse(200, {'data': notes}) if notes else FakeResponse(204)
        if call.method == 'GET' and len(parts) == 2:
            record = self.records.get((parts[0], parts[1]))
            return FakeResponse(200, {'data': [record]}) if record else FakeResponse(204)
        if call.method == 'POST' and path == 'Notes':
            item = call.json['data'][0]
            note_id = self.new_id()
            self.notes[item['Parent_Id']].append({'id': note_id, 'Note_Title': item['Note_Title']})
            return success(note_id)
        if call.method == 'POST' and path.endswith('/actions/add_tags'):
            return success(parts[1], 200)
        if call.method == 'PUT' and len(parts) == 1:
            return success(call.json['data'][0]['id'], 200)
        if call.method == 'POST' and len(parts) == 1:
            record_id = self.new_id()
            self.records[(path, record_id)] = {'id': record_id, **call.json['data'][0]}
            return success(record_id)
        raise NetworkBlocked(f'FakeCrm has no route for {call.method} {call.url}')


class PipelineTestCase(NoNetworkTestCase):
    """Warm token, GAQL metadata, the fake Zoho, and `crm.intake.start_intake` mocked until WP5 lands."""

    def setUp(self):
        super().setUp()
        self.warm_zoho_token()
        seed_gaql_schema()
        self.crm = FakeCrm()
        self.http.handler = self.crm
        self.start_intake = Mock(return_value=[])
        intake = patch.dict(sys.modules, {'crm.intake': SimpleNamespace(start_intake=self.start_intake)})
        intake.start()
        self.addCleanup(intake.stop)

    def calls(self, method: str, path: str) -> list[HttpCall]:
        return [call for call in self.http.calls
                if call.method == method and call.url.split('/crm/', 1)[1].partition('/')[2] == path]

    def ops_alerts(self, subject_part: str) -> list:
        return [message for message in mail.outbox if subject_part in message.subject]


# --- Steps -----------------------------------------------------------------------------------------------

class StepTests(PipelineTestCase):
    def test_each_step_skipped_when_result_persisted(self):
        quote = v5_quote(zoho_contact_id='c1', zoho_contact_action='matched', zoho_attribution_id='a1',
                         zoho_record_id='r1', zoho_note_id='n1')

        self.assertEqual(pipeline.run(quote.pk), 'synced')

        self.assertNoHttp()                      # phone_link finds no lead; intake is mocked
        self.start_intake.assert_called_once_with(quote.pk)
        quote.refresh_from_db()
        self.assertEqual((quote.sync_step, quote.sync_attempts), ('done', 0))

        QuoteSubmission.objects.filter(pk=quote.pk).update(sync_status='pending', sync_step='intake')
        self.start_intake.reset_mock()
        self.assertEqual(pipeline.run(quote.pk), 'synced')
        self.start_intake.assert_not_called()   # a completed step is never repeated
        self.assertNoHttp()

    def test_contact_found_fill_blanks_only_never_overwrites(self):
        quote = v5_quote()
        self.crm.contacts = [{'id': 'c9', 'Email': quote.email, 'First_Name': 'Mary', 'Phone': ''}]

        pipeline.step_contact(quote)

        (put,) = self.calls('PUT', 'Contacts')
        self.assertEqual(put.json, {'data': [{'id': 'c9', 'Phone': '+12025550147'}], 'trigger': []})
        self.assertEqual(self.calls('POST', 'Contacts'), [])
        quote.refresh_from_db()
        self.assertEqual((quote.zoho_contact_id, quote.zoho_contact_action), ('c9', 'matched'))

        self.crm.contacts = [{'id': 'c9', 'Email': quote.email, 'First_Name': 'Mary', 'Phone': '+12025550000'}]
        pipeline.step_contact(quote)
        self.assertEqual(len(self.calls('PUT', 'Contacts')), 1)   # nothing blank: no second PUT

    def test_contact_duplicate_data_research(self):
        quote = v5_quote()
        self.crm.fail[('GET', 'Contacts/search')] = deque([
            FakeResponse(204), FakeResponse(200, {'data': [{'id': 'c7', 'Phone': '+1', 'First_Name': 'M'}]})])
        self.crm.fail[('POST', 'Contacts')] = deque([item_error('DUPLICATE_DATA', {'api_name': 'Email'})])

        pipeline.step_contact(quote)

        quote.refresh_from_db()
        self.assertEqual((quote.zoho_contact_id, quote.zoho_contact_action), ('c7', 'matched'))
        self.assertEqual(len(self.calls('GET', 'Contacts/search')), 2)
        self.assertEqual(self.calls('POST', 'Contacts')[0].json['trigger'], [])

    def test_email_over_100_skips_contact(self):
        long_email = f'{"a" * 90}@example.com'
        quote = v5_quote(email=long_email, email_normalized=long_email)

        pipeline.step_contact(quote)

        self.assertNoHttp()
        quote.refresh_from_db()
        self.assertEqual((quote.zoho_contact_id, quote.zoho_contact_action), ('', 'skipped'))
        self.assertTrue(pipeline.is_done(quote, 'contact'))
        self.assertNotIn('Client_Email', gaql.build_gaql_record(quote))
        self.assertNotIn('Name_of_Client', gaql.build_gaql_record(quote))

    def test_attribution_uses_external_key_dcmn_attribution_qr_pk(self):
        quote = v5_quote()

        pipeline.step_attribution(quote)

        (post,) = self.calls('POST', 'Lead_Attribution_Records')
        self.assertEqual(post.json['data'][0]['External_Order_Key'], f'dcmn:attribution:qr:{quote.pk}')
        quote.refresh_from_db()
        self.assertTrue(quote.zoho_attribution_id)

    def test_attribution_created_via_crm_zoho_with_empty_trigger(self):
        quote = v5_quote()

        with patch('orders.zoho_sync.create_attribution_record') as legacy:
            pipeline.step_attribution(quote)

        legacy.assert_not_called()
        (post,) = self.http.calls
        self.assertEqual(post.url, f'{API}v2/Lead_Attribution_Records')
        self.assertEqual(post.json['trigger'], [])
        self.assertEqual(post.headers['Authorization'], 'Zoho-oauthtoken test-token')
        expected_url = quote.attribution_data.get('lead_url') or quote.page_url
        self.assertEqual(post.json['data'][0]['Lead_URL'], expected_url[:255])

    def test_attribution_name_matches_legacy_format(self):
        quote = v5_quote()

        pipeline.step_attribution(quote)

        record = self.calls('POST', 'Lead_Attribution_Records')[0].json['data'][0]
        key = pipeline.attribution_key(quote)
        # orders/zoho_sync.py create_attribution_record: f'DCMN Attribution {key.removeprefix("dcmn:attribution:")}'
        self.assertEqual(record['Name'], f'DCMN Attribution {key.removeprefix("dcmn:attribution:")}')
        self.assertEqual(record['Name'], f'DCMN Attribution qr:{quote.pk}')

    def test_attribution_skipped_for_test_quote(self):
        quote = v5_quote(is_test=True, full_name='ZZ TEST Maria')

        pipeline.step_attribution(quote)
        self.assertNoHttp()
        quote.refresh_from_db()
        self.assertEqual(quote.zoho_attribution_id, '')

        with override_settings(E2E_ATTRIBUTION=True):
            pipeline.step_attribution(quote)
        self.assertEqual(len(self.calls('POST', 'Lead_Attribution_Records')), 1)

    def test_attribution_non_blocking_after_3_failures(self):
        quote = v5_quote()
        self.crm.fail[('POST', 'Lead_Attribution_Records')] = FakeResponse(503, {'code': 'INTERNAL_ERROR'})

        statuses = []
        for _ in range(3):
            statuses.append(pipeline.run(quote.pk))
            QuoteSubmission.objects.filter(pk=quote.pk).update(sync_next_at=timezone.now())

        self.assertEqual(statuses, ['retry', 'retry', 'synced'])
        quote.refresh_from_db()
        self.assertEqual(quote.zoho_attribution_id, '')
        self.assertTrue(quote.zoho_record_id)
        self.assertNotIn('Attribution_Record', self.calls('POST', GAQL)[0].json['data'][0])
        self.assertEqual(len(self.calls('POST', 'Lead_Attribution_Records')), 3)

    def test_record_duplicate_data_resolves_by_key(self):
        quote = v5_quote()
        self.crm.fail[('POST', GAQL)] = deque([item_error('DUPLICATE_DATA', {'api_name': 'External_Order_Key',
                                                                              'id': '6686316000077700001'})])

        pipeline.step_record(quote)

        quote.refresh_from_db()
        self.assertEqual(quote.zoho_record_id, '6686316000077700001')
        (post,) = self.calls('POST', GAQL)
        self.assertEqual(post.json['data'][0]['External_Order_Key'], f'dcmn:qr:{quote.pk}')
        self.assertEqual(post.json['trigger'], [])

    def test_details_note_idempotent_by_title_prefix(self):
        quote = v5_quote(zoho_record_id='r1')
        title = pipeline.receipt_title(quote)
        self.assertEqual(title, f'{quote.reference} · {GAQL_TITLE}')

        pipeline.step_details_note(quote)
        pipeline.step_details_note(quote)       # a re-run after a lost response: the Note is found

        (post,) = self.calls('POST', 'Notes')
        self.assertEqual(post.json['data'][0]['Note_Title'], title)
        self.assertEqual(post.json['data'][0]['se_module'], GAQL)
        self.assertEqual(post.json['trigger'], [])
        self.assertIn(quote.reference, post.json['data'][0]['Note_Content'])
        quote.refresh_from_db()
        self.assertEqual(quote.zoho_note_id, self.crm.notes['r1'][0]['id'])

    def test_receipt_note_lists_contact_differences(self):
        """DESIGN §5.2 step 1: a matched Contact's other phone or name goes into the receipt Note."""
        quote = v5_quote(zoho_record_id='r1', zoho_contact_id='c9', zoho_contact_action='matched')
        self.crm.records[('Contacts', 'c9')] = {'id': 'c9', 'Full_Name': 'Mary Smith', 'Phone': '(202) 555-0000'}
        same = v5_quote(zoho_record_id='r2', zoho_contact_id='c8', zoho_contact_action='matched')
        self.crm.records[('Contacts', 'c8')] = {'id': 'c8', 'Full_Name': f'  {same.full_name.upper()} ',
                                                 'Phone': same.phone_raw}
        unreadable = v5_quote(zoho_record_id='r3', zoho_contact_id='c7', zoho_contact_action='matched')
        self.crm.fail[('GET', 'Contacts/c7')] = deque([FakeResponse(500, {'code': 'INTERNAL_ERROR'})])

        for each in (quote, same, unreadable):
            pipeline.step_details_note(each)

        first, second, third = (call.json['data'][0]['Note_Content'] for call in self.calls('POST', 'Notes'))
        self.assertIn('Contact on file has another phone: (202) 555-0000', first)
        self.assertIn('Contact on file has another name: Mary Smith', first)
        self.assertNotIn('Contact on file', second)
        self.assertNotIn('Contact on file', third)          # the Note is still posted
        self.assertEqual(self.calls('GET', 'Contacts/c9')[0].params['fields'],
                         'Full_Name,First_Name,Last_Name,Phone,Mobile')


# --- Retries and failure -----------------------------------------------------------------------------------

class RetryTests(PipelineTestCase):
    def test_transient_sets_retry_and_ladder_delay(self):
        quote = v5_quote()
        self.crm.fail[('POST', GAQL)] = FakeResponse(503, {'code': 'INTERNAL_ERROR'})

        before = timezone.now()
        self.assertEqual(pipeline.run(quote.pk), 'retry')
        quote.refresh_from_db()
        self.assertEqual((quote.sync_attempts, quote.sync_step, quote.sync_lease_until), (1, 'attribution', None))
        self.assertEqual(quote.sync_last_error, 'record: INTERNAL_ERROR')
        self.assertAlmostEqual((quote.sync_next_at - before).total_seconds(), 30, delta=5)

        QuoteSubmission.objects.filter(pk=quote.pk).update(sync_next_at=timezone.now())
        before = timezone.now()
        self.assertEqual(pipeline.run(quote.pk), 'retry')
        quote.refresh_from_db()
        self.assertEqual(quote.sync_attempts, 2)
        self.assertAlmostEqual((quote.sync_next_at - before).total_seconds(), 120, delta=5)
        self.assertEqual(len(self.calls('POST', 'Lead_Attribution_Records')), 1)   # done steps stay done
        self.assertEqual(self.ops_alerts('not synced'), [])

    def test_24h_total_marks_failed_and_alerts(self):
        self.assertLessEqual(pipeline.retry_window(9), pipeline.MAX_AGE)
        self.assertGreater(pipeline.retry_window(10), pipeline.MAX_AGE)
        quote = v5_quote(sync_status='retry', sync_attempts=9)
        self.crm.fail[('POST', GAQL)] = FakeResponse(503, {'code': 'INTERNAL_ERROR'})

        self.assertEqual(pipeline.run(quote.pk), 'failed')

        quote.refresh_from_db()
        self.assertEqual((quote.sync_status, quote.sync_attempts), ('failed', 10))
        self.assertIsNotNone(quote.sync_failed_at)
        (alert,) = self.ops_alerts('not synced')
        self.assertIn(quote.reference, alert.subject)
        self.assertIn('Retry from failed step', alert.body)
        self.assertNotIn(quote.email, alert.body)

    def test_permanent_marks_failed_and_alerts(self):
        quote = v5_quote()
        self.crm.fail[('POST', GAQL)] = item_error('INVALID_DATA', {'api_name': 'Client_Phone'})

        self.assertEqual(pipeline.run(quote.pk), 'failed')

        quote.refresh_from_db()
        self.assertEqual(quote.sync_last_error, 'record: INVALID_DATA (Client_Phone)')
        self.assertEqual(quote.sync_step, 'attribution')
        self.assertEqual(len(self.calls('POST', GAQL)), 1)
        self.assertEqual(len(self.ops_alerts('not synced')), 1)
        self.start_intake.assert_not_called()


class LeaseTests(PipelineTestCase):
    def test_second_claim_returns_false(self):
        quote = v5_quote()

        self.assertTrue(pipeline.claim(quote.pk))
        self.assertFalse(pipeline.claim(quote.pk))
        self.assertEqual(pipeline.run(quote.pk), 'running')   # the second worker makes no call
        self.assertNoHttp()

        QuoteSubmission.objects.filter(pk=quote.pk).update(
            sync_status='retry', sync_lease_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(pipeline.claim(quote.pk))             # an expired lease can be taken


class CircuitTests(PipelineTestCase):
    def test_circuit_open_no_http_goes_retry(self):
        quote = v5_quote()
        cache.set(zoho.CIRCUIT_KEY, time.time() + 60, 60)

        self.assertEqual(pipeline.run(quote.pk), 'retry')

        self.assertNoHttp()
        quote.refresh_from_db()
        self.assertEqual((quote.sync_attempts, quote.sync_last_error), (1, 'contact: CIRCUIT_OPEN'))
        self.assertEqual(self.ops_alerts('not synced'), [])


class PausedTests(PipelineTestCase):
    def test_sync_off_saves_paused_and_reconciler_rearms_when_on(self):
        quote = v5_quote()

        with override_settings(QUOTE_V3_ZOHO_SYNC=False):
            self.assertEqual(process_quote_submission.apply((quote.pk,)).get(), 'paused')
            with patch.object(process_quote_submission, 'delay') as delay:
                pipeline.reconcile()
            delay.assert_not_called()
        self.assertNoHttp()
        quote.refresh_from_db()
        self.assertEqual(quote.sync_status, 'paused')

        with patch.object(process_quote_submission, 'delay') as delay:
            counts = pipeline.reconcile()

        self.assertEqual(counts['rearmed'], 1)
        delay.assert_called_once_with(quote.pk)
        quote.refresh_from_db()
        self.assertEqual(quote.sync_status, 'pending')
        self.assertEqual(pipeline.run(quote.pk), 'synced')


class PublishFailureTests(PipelineTestCase):
    def test_celery_publish_error_sends_staff_email_synchronously(self):
        body = make_body()
        meta = RequestMeta(ip='203.0.113.9', user_agent='test', turnstile_result='passed', attribution=None)

        with patch.object(process_quote_submission, 'delay', side_effect=OperationalError('broker down')), \
                self.captureOnCommitCallbacks(execute=True):
            quote = save_submission(normalised(body), meta).quote

        staff = [m for m in mail.outbox if m.subject.startswith('❓ New Quote Request')]
        self.assertEqual(len(staff), 1)
        self.assertIn(quote.reference, staff[0].body)
        quote.refresh_from_db()
        self.assertEqual((quote.staff_email_status, quote.sync_status), ('sent', 'pending'))
        self.assertNoHttp()


# --- Reconciler --------------------------------------------------------------------------------------------

class ReconcilerTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        for task in (process_quote_submission, send_quote_staff_email, send_quote_confirmation_email):
            patcher = patch.object(task, 'delay')
            setattr(self, f'{task.name.rsplit(".", 1)[1]}_delay', patcher.start())
            self.addCleanup(patcher.stop)

    def age(self, quote: QuoteSubmission, minutes: int) -> None:
        QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))

    def test_republishes_due_rows(self):
        now = timezone.now()
        pending = make_quote_submission(staff_email_status='sent')
        due = make_quote_submission(sync_status='retry', sync_next_at=now - timedelta(seconds=1),
                                    staff_email_status='sent')
        make_quote_submission(sync_status='retry', sync_next_at=now + timedelta(minutes=5), staff_email_status='sent')
        make_quote_submission(sync_status='synced', staff_email_status='sent')
        make_quote_submission(sync_status='held', staff_email_status='skipped')
        stale = make_quote_submission(sync_status='running', sync_lease_until=now - timedelta(minutes=1),
                                      staff_email_status='sent')

        counts = pipeline.reconcile()

        published = sorted(call.args[0] for call in self.process_quote_submission_delay.call_args_list)
        self.assertEqual(published, sorted([pending.pk, due.pk, stale.pk]))
        self.assertEqual((counts['published'], counts['recovered']), (3, 1))
        stale.refresh_from_db()
        self.assertEqual(stale.sync_status, 'retry')
        self.assertNoHttp()

    def test_unsynced_30m_alert_once(self):
        quote = make_quote_submission(staff_email_status='sent')
        self.age(quote, 31)
        fresh = make_quote_submission(staff_email_status='sent')
        self.age(fresh, 10)

        self.assertEqual(pipeline.reconcile()['alerts'], 1)
        cache.clear()                                  # the once is the row's, not the alert dedupe's
        self.assertEqual(pipeline.reconcile()['alerts'], 0)

        (alert,) = self.ops_alerts('not in Zoho after 30 min')
        self.assertIn(quote.reference, alert.subject)
        quote.refresh_from_db()
        self.assertIsNotNone(quote.alerted_unsynced_at)

    def test_staff_email_watchdog_after_3min(self):
        quote = make_quote_submission(sync_status='synced')
        self.age(quote, 2)

        pipeline.reconcile()
        self.send_quote_staff_email_delay.assert_not_called()

        self.age(quote, 4)
        self.assertEqual(pipeline.reconcile()['staff_emails'], 1)
        self.send_quote_staff_email_delay.assert_called_once_with(quote.pk)
        self.send_quote_confirmation_email_delay.assert_not_called()   # confirmation is `off`

    def test_stale_sending_email_recovered(self):
        stuck = make_quote_submission(sync_status='synced', staff_email_status='sending')
        QuoteSubmission.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(minutes=11))
        self.age(stuck, 12)
        busy = make_quote_submission(sync_status='synced', staff_email_status='sending')
        self.age(busy, 12)

        counts = pipeline.reconcile()

        self.assertEqual((counts['emails_recovered'], counts['staff_emails']), (1, 1))
        self.send_quote_staff_email_delay.assert_called_once_with(stuck.pk)
        self.assertEqual(QuoteSubmission.objects.get(pk=busy.pk).staff_email_status, 'sending')


# --- Trigger policy over a full run -----------------------------------------------------------------------

class TriggerTests(PipelineTestCase):
    def test_every_zoho_write_has_empty_trigger(self):
        from quotes.tests.test_phone_match import phone_lead

        quote = v5_quote(spam_status='suspect', spam_reasons=['turnstile_failed'])
        self.crm.contacts = [{'id': 'c9', 'Email': quote.email, 'First_Name': '', 'Phone': ''}]
        phone_lead(phone='202-555-0147', module='Apostille_Services', record_id='6686316000055500001',
                   created_at=quote.created_at - timedelta(days=1))
        self.crm.records[('Apostille_Services', '6686316000055500001')] = {
            'id': '6686316000055500001', 'Status': 'Phone Call Received'}
        QuoteSubmission.objects.filter(pk=quote.pk).update(amendments=[
            {'at': '2026-11-26T15:00:00Z', 'changed': ['message'], 'payload': {'contact': {'message': 'later'}}}])

        self.assertEqual(pipeline.run(quote.pk), 'synced')

        writes = self.http.writes
        paths = {call.url.split('/crm/v2/', 1)[1] for call in writes}
        quote.refresh_from_db()
        self.assertTrue({'Contacts', 'Lead_Attribution_Records', GAQL, 'Notes',
                         f'{GAQL}/{quote.zoho_record_id}/actions/add_tags'} <= paths, paths)
        for call in writes:
            with self.subTest(write=f'{call.method} {call.url}'):
                self.assertEqual(call.json['trigger'], [])
        for call in writes:
            if call.method == 'POST' and call.url.split('/crm/v2/', 1)[1] in CREATE_PATHS:
                self.assertTrue(call.json['data'][0]['External_Order_Key'].startswith('dcmn:'))
        self.assertEqual(len(self.calls('POST', 'Notes')), 4)   # receipt, amendment, phone lead both ways
        self.assertTrue(all(call.method == 'GET' for call in self.http.calls if call not in writes))


class IntakeIntegrationTests(PipelineTestCase):
    def test_intake_step_calls_start_intake(self):
        quote = v5_quote(zoho_contact_action='matched', zoho_attribution_id='a1', zoho_record_id='r1',
                         zoho_note_id='n1', intake_mode='none')
        with patch.dict(sys.modules):
            if WP5_INTAKE.exists():    # WP5 has landed: the real interface, spied on (mode `none` creates nothing)
                sys.modules.pop('crm.intake')
                real = importlib.import_module('crm.intake').start_intake
                self.start_intake.side_effect = real
                sys.modules['crm.intake'] = SimpleNamespace(start_intake=self.start_intake)

            self.assertEqual(pipeline.run(quote.pk), 'synced')

        self.start_intake.assert_called_once_with(quote.pk)
        quote.refresh_from_db()
        self.assertEqual(quote.sync_step, 'done')

        QuoteSubmission.objects.filter(pk=quote.pk).update(sync_status='pending', sync_step='phone_link')
        self.start_intake.side_effect = RuntimeError('engine down')
        self.assertEqual(pipeline.run(quote.pk), 'retry')     # intake blocks: the quote is not `synced`
        self.assertEqual(QuoteSubmission.objects.get(pk=quote.pk).sync_last_error, 'intake: RuntimeError')


# --- Admin actions and the reconcile command --------------------------------------------------------------

class AdminActionTests(PipelineTestCase):
    URL = '/admin/quotes/quotesubmission/'

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.admin = get_user_model().objects.create_superuser('wp4-admin', 'admin@example.com', 'unused-pass-1')
        self.client.force_login(self.admin)
        for task in (process_quote_submission, send_quote_staff_email, send_quote_confirmation_email):
            patcher = patch.object(task, 'delay')
            setattr(self, f'{task.name.rsplit(".", 1)[1]}_delay', patcher.start())
            self.addCleanup(patcher.stop)

    def act(self, action: str, *quotes: QuoteSubmission):
        response = self.client.post(self.URL, {'action': action, '_selected_action': [q.pk for q in quotes]})
        self.assertEqual(response.status_code, 302)
        for quote in quotes:
            quote.refresh_from_db()

    def test_retry_release_spam_and_resend(self):
        failed = make_quote_submission(sync_status='failed', sync_attempts=4, staff_email_status='sent')
        held = make_quote_submission(sync_status='held', spam_status='held', staff_email_status='skipped')
        fresh = make_quote_submission()
        synced = make_quote_submission(sync_status='synced', zoho_record_id='r1', staff_email_status='sent')

        self.act('retry_from_failed_step', failed, synced)
        self.assertEqual((failed.sync_status, failed.sync_attempts, synced.sync_status), ('pending', 0, 'synced'))
        self.process_quote_submission_delay.assert_called_once_with(failed.pk)

        self.act('release_from_hold', held)
        self.assertEqual((held.sync_status, held.spam_status, held.staff_email_status),
                         ('pending', 'released', 'pending'))
        self.send_quote_staff_email_delay.assert_called_once_with(held.pk)

        self.act('mark_spam', fresh, synced)
        self.assertEqual((fresh.sync_status, fresh.spam_status, fresh.staff_email_status), ('spam', 'spam', 'skipped'))
        self.assertEqual(synced.sync_status, 'synced')            # a quote in Zoho is left to the managers

        self.act('resend_staff_email', synced, fresh)
        self.assertEqual((synced.staff_email_status, fresh.staff_email_status), ('pending', 'skipped'))
        self.send_quote_staff_email_delay.assert_called_with(synced.pk)

    def test_released_quote_gets_its_one_confirmation(self):
        held = make_quote_submission(sync_status='held', spam_status='held', staff_email_status='skipped',
                                     confirmation_email_status='off', is_test=False, full_name='Maria Fernandez')
        test_quote = make_quote_submission(sync_status='held', spam_status='held', staff_email_status='skipped',
                                           confirmation_email_status='off')

        with override_settings(QUOTE_V3_CONFIRMATION_EMAIL=True):
            self.act('release_from_hold', held, test_quote)

        self.assertEqual((held.spam_status, held.confirmation_email_status), ('released', 'pending'))
        self.assertEqual(test_quote.confirmation_email_status, 'off')   # ZZ TEST: no client email
        self.send_quote_confirmation_email_delay.assert_called_once_with(held.pk)

        again = make_quote_submission(sync_status='held', spam_status='held', staff_email_status='skipped',
                                      is_test=False)
        self.act('release_from_hold', again)                                # the flag is off by default
        self.assertEqual(again.confirmation_email_status, 'off')
        self.send_quote_confirmation_email_delay.assert_called_once()

    def test_rerun_intake_and_erase(self):
        quote = make_quote_submission(sync_status='synced', zoho_record_id='r1')
        not_in_zoho = make_quote_submission()

        self.act('rerun_intake', quote, not_in_zoho)
        self.start_intake.assert_called_once_with(quote.pk)

        self.act('erase_pii', quote)
        self.assertEqual((quote.full_name, quote.email), ('', ''))
        self.assertIsNotNone(quote.pii_erased_at)
        self.assertNoHttp()


class ReconcileCommandTests(PipelineTestCase):
    def test_rearms_failed_since_day_never_zoho_sync_jobs(self):
        from io import StringIO

        from django.core.management import call_command
        from orders.models import ZohoSyncJob

        job = ZohoSyncJob.objects.create(order_type='quote', order_id=1, zoho_module=GAQL, status='failed')
        recent = make_quote_submission(sync_status='failed', staff_email_status='sent')
        old = make_quote_submission(sync_status='failed', staff_email_status='sent')
        QuoteSubmission.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        since = (timezone.now() - timedelta(days=2)).strftime('%Y-%m-%d')

        with patch.object(process_quote_submission, 'delay') as delay:
            out = StringIO()
            call_command('quotes_reconcile', '--since', since, '--dry-run', stdout=out)
            self.assertIn(f'would re-arm {recent.reference}', out.getvalue())
            self.assertEqual(QuoteSubmission.objects.get(pk=recent.pk).sync_status, 'failed')

            call_command('quotes_reconcile', '--since', since, stdout=StringIO())

        delay.assert_called_once_with(recent.pk)
        self.assertEqual(QuoteSubmission.objects.get(pk=recent.pk).sync_status, 'pending')
        self.assertEqual(QuoteSubmission.objects.get(pk=old.pk).sync_status, 'failed')
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
