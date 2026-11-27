"""Regression tests of the review of 2026-11-27 (IMPLEMENTATION-STATUS "Review 2026-11-27"); one per finding id."""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import requests
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from crm import adoption, engine, limits, rules, sources, translation_capture as tc, zoho
from crm.models import EngineRequest, ServiceLink, ServiceSignal
from crm.tasks import reconcile_service_links, run_service_link, writeback_source
from crm.tasks.ops import ops_daily_digest
from crm.tasks.translation import apply_signal
from crm.testing import FakeResponse, NoNetworkTestCase
from crm.tests.support import EngineTestCase, make_link, saved_quote
from crm.tests.test_models import FLAGS_OFF
from crm.writeback import write_back
from crm.zoho import KEY_FIELD
from orders.models import Track
from quotes.models import GAQL_MODULE


class LostCreateTests(EngineTestCase):
    """correctness-1: a key hit on our own earlier POST finishes the create (Track, email, cap)."""

    @override_settings(ENGINE_TID_CREATED_EMAIL='first_per_quote', QUOTE_V3_TEST_EMAILS=True)
    def test_lost_create_response_finishes_as_create(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille')
        lost = []

        def store_then_time_out(call):
            if call.method == 'POST' and call.url.endswith('/v2/Apostille_Services') and not lost:
                lost.append(self.crm(call))   # Zoho created it, the answer never arrived
                raise requests.Timeout()
            return self.crm(call)

        self.http.handler = store_then_time_out
        self.assertEqual(engine.run_link(link.pk), 'retry')
        link.refresh_from_db()
        self.assertTrue(link.tracking_id and link.payload_sha256)

        with patch('crm.tid.send_tracking_email_task') as email:
            self.assertEqual(engine.run_link(link.pk), 'created')

        link.refresh_from_db()
        (record_id,) = self.crm.records['Apostille_Services']
        self.assertEqual((link.state, link.origin, link.target_record_id), ('created', 'created', record_id))
        self.assertEqual(len(self.creates('Apostille_Services')), 1)
        self.assertEqual(Track.objects.get(tid=link.tracking_id).data['record_id'], record_id)
        quote.refresh_from_db()
        self.assertEqual(quote.created_email_tid, link.tracking_id)
        email.delay.assert_called_once_with(link.tracking_id, 'created')
        per_source_key, _ = limits._keys(link.source_key)
        self.assertEqual(cache.get(per_source_key), 1)

    def test_key_found_without_our_post_stays_adopted_key(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille', state='retry')   # no payload_sha256: no POST of ours (restored DB)
        record = self.crm.add('Apostille_Services', **{KEY_FIELD: link.external_key, 'Name': 'Apostille · x'})

        self.assertEqual(engine.run_link(link.pk), 'adopted')

        link.refresh_from_db()
        self.assertEqual((link.origin, link.target_record_id), ('adopted_key', record['id']))


class BookkeepingTests(EngineTestCase):
    """correctness-5: a failure after the create never flips the link, and an unqueued email is released."""

    def test_count_failure_keeps_created_link(self):
        link = make_link(saved_quote(), 'apostille')

        with patch('crm.engine.limits.count_create', side_effect=ConnectionError('redis down')), \
                self.assertLogs('crm.engine', 'ERROR'):
            self.assertEqual(engine.run_link(link.pk), 'created')

        link.refresh_from_db()
        self.assertEqual((link.state, link.attempts), ('created', 1))
        self.assertTrue(link.target_record_id)
        self.assertEqual(Track.objects.get(tid=link.tracking_id).data['record_id'], link.target_record_id)
        self.assertTrue(any('bookkeeping' in message.subject for message in mail.outbox))

    @override_settings(ENGINE_TID_CREATED_EMAIL='first_per_quote', QUOTE_V3_TEST_EMAILS=True)
    def test_unqueued_created_email_releases_the_claim(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille')

        with patch('crm.tid.send_tracking_email_task') as email, self.assertLogs('crm.engine', 'ERROR'):
            email.delay.side_effect = ConnectionError('broker down')
            self.assertEqual(engine.run_link(link.pk), 'created')

        quote.refresh_from_db()
        self.assertEqual(quote.created_email_tid, '')   # a later link of the quote may still send it


class CopyClaimTests(EngineTestCase):
    """correctness-4: two links of one module running together cannot both adopt one workflow copy."""

    def test_parallel_links_cannot_adopt_the_same_copy(self):
        quote = saved_quote('svc_fbi')
        copy = self.crm.add('FINGERPRINT_SERVICES', Name='Fingerprinting Services')
        first, second = make_link(quote, 'fbi'), make_link(quote, 'fingerprinting')

        # Both read the timeline before either saved: each sees the same free copy.
        with patch('crm.engine.adoption.find_existing',
                   side_effect=lambda *a, **k: adoption.Adoption(copy['id'], 'adopted_copy')), \
                patch('crm.engine._contact'):
            self.assertEqual(engine.run_link(first.pk), 'adopted')
            self.assertEqual(engine.run_link(second.pk), 'created')

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.target_record_id, copy['id'])
        self.assertNotEqual(second.target_record_id, copy['id'])
        self.assertEqual(len(self.creates('FINGERPRINT_SERVICES')), 1)


class WritebackMergeTests(EngineTestCase):
    """correctness-3: a running write-back never drops keys a concurrent _adopt saved."""

    def test_writeback_keeps_concurrent_duplicate_copies(self):
        quote = saved_quote()
        self.gaql_record(quote)
        link = make_link(quote, 'apostille', state='creating', lease_until=timezone.now() + timedelta(seconds=30))
        original = zoho.update

        def concurrent_adopt(module, record_id, fields, **kwargs):
            if module == GAQL_MODULE:   # the GAQL PUT of the write-back: meanwhile the link's task adopts
                ServiceLink.objects.filter(pk=link.pk).update(writeback={'duplicate_copies': ['X1']}, state='adopted')
            return original(module, record_id, fields, **kwargs)

        with patch('crm.writeback.zoho.update', side_effect=concurrent_adopt):
            write_back(link.source_key)

        link.refresh_from_db()
        self.assertEqual(link.writeback['duplicate_copies'], ['X1'])
        self.assertEqual(link.writeback['source_lines']['state'], 'done')


class DedupeRaceTests(EngineTestCase):
    """correctness-8: two clicks inserted together: the earlier one runs, only the later one is a duplicate."""

    def test_simultaneous_twins_first_proceeds(self):
        quote = saved_quote()
        create = EngineRequest.objects.create
        twins = []

        def create_with_twin(**fields):
            row = create(**fields)
            if not twins:   # the other worker's insert lands before this request's dedupe query
                twins.append(create(**fields))
            return row

        with patch.object(EngineRequest.objects, 'create', side_effect=create_with_twin):
            result = engine.handle(sources.for_quote(quote), ['apostille'], mode='create', trigger='zoho_button',
                                   caller='zoho', actor={}, client_request_id='click-twin')

        self.assertNotEqual(result.request.error_code, 'duplicate_request')
        self.assertEqual(result.body['results'][0]['status'], 'created')
        twin_answer = engine._handle(twins[0], sources.for_quote(quote), ['apostille'], mode='create',
                                     trigger='zoho_button', caller='zoho', actor={}, options={}, started=0.0,
                                     budget_s=None)
        self.assertEqual(twin_answer.request.error_code, '')   # the first one is terminal now: it answers exists


class LeaseResetTests(EngineTestCase):
    """correctness-11: a click never resets a link a runner leased after the click read it."""

    def test_reset_never_takes_a_leased_link(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille', state='retry', attempts=1)
        original = engine._link_for

        def runner_takes_it(call, key):
            found = original(call, key)
            engine.lease(link.pk)
            return found

        with patch('crm.engine._link_for', side_effect=runner_takes_it):
            result = engine.handle(sources.for_quote(quote), ['apostille'], mode='create', trigger='zoho_button',
                                   caller='zoho', actor={})

        self.assertEqual((result.http_status, result.body['results'][0]['status']), (409, 'in_progress'))
        link.refresh_from_db()
        self.assertEqual(link.state, 'creating')
        self.assertIsNotNone(link.lease_until)
        self.assertFalse(engine.lease(link.pk))
        self.assertEqual(self.creates('Apostille_Services'), [])


@override_settings(CRM_ENGINE_MODE='dry_run')
class DryRunTests(EngineTestCase):
    """correctness-10: dry_run stops queued links, the reconciler and write-backs too."""

    def test_dry_run_runs_no_link(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille', state='retry', attempts=1,
                         next_attempt_at=timezone.now() - timedelta(minutes=1))

        self.assertEqual(reconcile_service_links(), 'skipped')
        self.assertEqual(run_service_link(link.pk), 'skipped')
        self.assertEqual(engine.run_link(link.pk), 'retry')
        self.assertEqual(writeback_source(link.source_key), 'skipped')

        link.refresh_from_db()
        self.assertEqual((link.state, link.attempts), ('retry', 1))
        self.assertEqual(self.zoho_writes(), [])


class RuleRetryTests(EngineTestCase):
    """correctness-7: an engine that cannot answer leaves the signal open; it is not `ignored` for good."""

    def signal(self, **fields) -> ServiceSignal:
        values = {'dedupe_key': 'translation_requested:form:apostille:1:translation', 'kind': 'translation_requested',
                  'source_kind': 'form', 'source_key': 'form:apostille:1', 'source_module': 'Apostille_Services',
                  'source_record_id': '7100000000000000777', 'order_type': 'apostille', 'order_id': 1,
                  'service_key': 'translation', 'state': 'fields_written', **fields}
        return ServiceSignal.objects.create(**values)

    @override_settings(CRM_ENGINE_MODE='off')
    def test_engine_off_keeps_signal_open(self):
        signal = self.signal()

        self.assertEqual(rules.evaluate(signal), 'retry')

        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.outcome, signal.attempts, signal.last_error),
                         ('fields_written', '', 1, 'disabled'))
        capped = self.signal(dedupe_key='translation_requested:form:apostille:2:translation',
                             attempts=rules.RETRY_MAX_ATTEMPTS - 1)
        self.assertEqual(rules.evaluate(capped), 'ignored')   # bounded: it ends after RETRY_MAX_ATTEMPTS
        capped.refresh_from_db()
        self.assertEqual(capped.state, 'evaluated')

    @override_settings(CRM_ENGINE_MODE='off', TRANSLATION_CAPTURE_ENABLED=True)
    def test_apply_signal_waits_while_engine_off(self):
        signal = self.signal()

        self.assertEqual(apply_signal(signal), 'waiting')

        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.attempts), ('fields_written', 0))
        self.assertFalse(EngineRequest.objects.exists())


class IgnoredCounterDayTests(NoNetworkTestCase):
    """correctness-9: the counter is keyed by the ET day the digest reads."""

    def test_ignored_counter_uses_et_day(self):
        evening_et = datetime(2026, 11, 27, 2, 0, tzinfo=dt_timezone.utc)   # 21:00 ET on 26 Nov

        with patch('crm.translation_capture.timezone.now', return_value=evening_et):
            tc.count_ignored('marriage')

        self.assertEqual(tc.ignored_counts(date(2026, 11, 26)), {'marriage': 1})
        self.assertEqual(tc.ignored_counts(date(2026, 11, 27)), {})


class ColdTokenTests(EngineTestCase):
    """zoho-contract-1: a cold token on a button click is refreshed within the budget and the click queues."""

    def test_cold_token_click_refreshes_within_budget_and_queues(self):
        quote = saved_quote()
        self.gaql_record(quote)
        cache.delete(engine.TOKEN_CACHE_KEY)

        with self.captureOnCommitCallbacks():
            result = engine.handle((GAQL_MODULE, quote.zoho_record_id), ['apostille'], mode='create',
                                   trigger='zoho_button', caller='zoho', actor={})

        self.assertEqual((result.http_status, result.body['results'][0]['status']), (202, 'queued'))
        (refresh,) = [c for c in self.http.calls if c.url.startswith('https://accounts.zoho.com/')]
        self.assertEqual(refresh.timeout[0], zoho.INLINE_CONNECT_S)
        self.assertLessEqual(refresh.timeout[1], zoho.INLINE_READ_MAX_S)
        self.assertTrue(all(c.timeout[1] <= zoho.INLINE_READ_MAX_S for c in self.http.calls))
        self.assertEqual(self.creates('Apostille_Services'), [])   # no inline create on a cold token
        self.assertEqual(cache.get(engine.TOKEN_CACHE_KEY), 'refreshed-token')

    def test_fast_call_does_not_refresh_on_401(self):
        self.http.handler = None
        self.http.queue(FakeResponse(401, {'code': 'INVALID_TOKEN'}))

        with self.assertRaises(zoho.ZohoTransient), self.assertLogs('crm.zoho', 'WARNING'):
            zoho.get(GAQL_MODULE, '6686316000000000001', timeout=(1.0, 4.0))

        self.assertEqual(len(self.http.calls), 1)          # no OAuth refresh, no second GET
        self.assertIsNone(cache.get(engine.TOKEN_CACHE_KEY))   # the refused token is dropped


@override_settings(**FLAGS_OFF, OPS_ALERT_EMAILS=[], EMAIL_OFFICE_RECEIVER=['office@example.com'])
class DigestGateTests(NoNetworkTestCase):
    """legacy-rollout-1: with every flag at its default the digest mails nobody."""

    def test_digest_skipped_while_every_feature_is_off(self):
        self.assertEqual(ops_daily_digest(), 'skipped')
        self.assertEqual(mail.outbox, [])

        with override_settings(CRM_ENGINE_MODE='live'):
            self.assertEqual(ops_daily_digest(), 'sent')
        (digest,) = mail.outbox
        self.assertEqual(digest.to, ['office@example.com'])

