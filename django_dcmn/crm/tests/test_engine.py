"""`crm.engine.handle` and `run_link` (DESIGN §7.7, §7.8, §8.2): results, expansion, limits, inline budget, modes."""

import threading
import time
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

import requests
from django.core import mail
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models.query import QuerySet
from django.test import override_settings, tag
from django.utils import timezone

from crm import engine, messages, registry, schema, sources
from crm.models import EngineRequest, ServiceLink
from crm.tasks import reconcile_service_links
from crm.testing import FakeResponse
from crm.tests.support import (
    CONTACT_ID, GAQL_ID, EngineTestCase, EngineTransactionTestCase, make_link, saved_quote,
)
from crm.zoho import CIRCUIT_KEY, KEY_FIELD, TOKEN_CACHE_KEY, TOKEN_COOLDOWN_KEY
from orders.models import Track
from quotes.models import GAQL_MODULE, QuoteLine


def rename_to_test(body: dict) -> None:
    body['contact']['full_name'] = 'ZZ TEST Maria Fernández'


class EngineCase(EngineTestCase):
    def handle(self, source, services, **kwargs) -> engine.EngineResult:
        if hasattr(source, 'lines'):
            source = sources.for_quote(source)
        values = {'mode': 'create', 'trigger': 'zoho_button', 'caller': 'zoho', 'actor': {'initials': 'VL'},
                  **kwargs}
        return engine.handle(source, services, **values)

    def one(self, result: engine.EngineResult) -> dict:
        (only,) = result.body['results']
        return only


class ResultTests(EngineCase):
    def test_created(self):
        quote = saved_quote()

        result = self.handle(quote, ['apostille'])

        self.assertEqual(result.http_status, 200)
        item = self.one(result)
        self.assertEqual(item['status'], 'created')
        link = ServiceLink.objects.get()
        self.assertEqual((link.state, link.origin, link.external_key),
                         ('created', 'created', f'dcmn:qr:{quote.pk}:apostille'))
        self.assertEqual(item['url'], f'https://crm.zoho.com/crm/org882204370/tab/CustomModule2/{link.target_record_id}')
        self.assertEqual(result.body['open_url'], item['url'])
        self.assertEqual(result.body['state'], 'done')
        self.assertEqual(result.body['message'], 'Apostille created in Apostille Services. Opening it.')
        (post,) = self.creates('Apostille_Services')
        self.assertEqual(post.json['trigger'], [])
        self.assertEqual(post.json['data'][0][KEY_FIELD], link.external_key)
        self.assertEqual(post.json['data'][0]['Client_Contact'], {'id': CONTACT_ID})
        self.assertEqual(result.request.state, 'done')

    def test_exists(self):
        quote = saved_quote()
        self.handle(quote, ['apostille'])
        self.http.calls.clear()

        result = self.handle(quote, ['apostille'], trigger='create_all')

        item = self.one(result)
        self.assertEqual((result.http_status, item['status']), (200, 'exists'))
        self.assertRegex(result.body['message'],
                         r'^Apostille already exists for this quote \(created \d+ \w{3} by button\)\. Opening it\.$')
        self.assertEqual(self.zoho_writes(), [])
        self.assertEqual(result.body['open_url'], item['url'])

    def test_target_deleted(self):
        quote = saved_quote()
        link = ServiceLink.objects.get(pk=self.one(self.handle(quote, ['apostille']))['link_id'])
        del self.crm.records['Apostille_Services'][link.target_record_id]

        result = self.handle(quote, ['apostille'])

        self.assertEqual((result.http_status, self.one(result)['status']), (200, 'target_deleted'))
        self.assertEqual(result.body['message'],
                         'The Apostille record linked to this quote was deleted. Use Add service… to create it again.')
        self.assertEqual(len(self.creates('Apostille_Services')), 1)   # never re-created silently

    def test_lease_held_409(self):
        quote = saved_quote()
        make_link(quote, 'apostille', state='creating', lease_until=timezone.now() + timedelta(seconds=30))

        result = self.handle(quote, ['apostille'])

        self.assertEqual(result.http_status, 409)
        self.assertEqual(self.one(result)['status'], 'in_progress')
        self.assertEqual(result.body['message'], messages.IN_PROGRESS)
        self.assertEqual(result.request.error_code, 'in_progress')
        self.assertNoHttp()

    def test_no_target(self):
        quote = saved_quote('svc_retrieval')

        result = self.handle(quote, ['retrieval'])

        self.assertEqual((result.http_status, self.one(result)['status']), (200, 'no_target'))
        self.assertIn('has no pipeline yet', result.body['message'])
        self.assertFalse(ServiceLink.objects.exists())
        self.assertNoHttp()

    @override_settings(CRM_ENGINE_MODE='off')
    def test_disabled_503(self):
        result = self.handle(saved_quote(), ['apostille'])

        self.assertEqual(result.http_status, 503)
        self.assertEqual((result.body['detail'], result.body['message']), ('disabled', messages.DISABLED))
        self.assertEqual((result.request.state, result.request.error_code), ('refused', 'disabled'))
        self.assertNoHttp()

    def test_test_only_403(self):
        quote = saved_quote('v5_success')   # Maria Fernández: not a ZZ TEST client

        result = self.handle(quote, ['apostille'])

        self.assertEqual((result.http_status, result.body['detail']), (403, 'test_only'))
        self.assertEqual(result.body['message'], messages.TEST_ONLY)
        with override_settings(CRM_ENGINE_TEST_ONLY=False):
            self.assertEqual(self.handle(quote, ['apostille']).http_status, 200)

    def test_refused_spam(self):
        quote = saved_quote(spam_status='spam')

        result = self.handle(quote, ['apostille'])

        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.body['state'], 'refused')
        self.assertEqual(self.one(result)['status'], 'refused_spam')
        self.assertEqual(result.body['message'], messages.REFUSED_SPAM)
        self.assertFalse(ServiceLink.objects.exists())
        # A legacy record in the spam bucket is refused too; the panel may force it.
        record = self.crm.add(GAQL_MODULE, Client_Name='ZZ TEST spam', Quote_Status='Spam',
                              GET_A_QUOTE_LEADS='Apostille Service')
        legacy = self.handle((GAQL_MODULE, record['id']), ['apostille'])
        self.assertEqual(self.one(legacy)['status'], 'refused_spam')
        forced = self.handle((GAQL_MODULE, record['id']), ['apostille'], caller='panel',
                             options={'force_spam': True})
        self.assertEqual(self.one(forced)['status'], 'queued')

    def test_degraded_on_blocked_stage(self):
        key = schema.CACHE_KEY.format(module='Apostille_Services')
        fields = cache.get(key)
        fields['Status'] = {**fields['Status'], 'pick_list_values': [
            v for v in fields['Status']['pick_list_values'] if v != 'APOSTILE LEADS FROM GET QUOTE PIPELINE']}
        cache.set(key, fields)
        quote = saved_quote()

        result = self.handle(quote, ['apostille'])

        item = self.one(result)
        self.assertEqual((item['status'], item['error_code'], item['error_field']), ('degraded', 'schema_drift', 'Status'))
        self.assertTrue(result.body['message'].startswith('Zoho setup changed (Apostille stage list). Nothing was created.'))
        self.assertEqual(self.creates('Apostille_Services'), [])
        self.assertEqual(Track.objects.count(), 0)
        (alert,) = mail.outbox
        self.assertIn('schema drift', alert.subject.lower())

    def test_permanent_failed_with_code(self):
        self.crm.fail[('POST', 'Apostille_Services')] = [FakeResponse(400, {'data': [{
            'code': 'INVALID_DATA', 'status': 'error', 'details': {'api_name': 'Phone_Number'}}]})]

        result = self.handle(saved_quote(), ['apostille'])

        item = self.one(result)
        self.assertEqual((item['status'], item['error_code'], item['error_field']),
                         ('failed', 'INVALID_DATA', 'Phone_Number'))
        self.assertEqual(result.body['message'],
                         f'Apostille could not be created: Zoho said INVALID_DATA (Phone_Number). Nothing was '
                         f'duplicated. Tell Vlad, ref {result.request.ref}.')
        self.assertEqual((result.request.state, result.request.error_code), ('failed', 'INVALID_DATA'))
        self.assertEqual(len(self.creates('Apostille_Services')), 1)


class TokenThrottleTests(EngineCase):
    def test_throttled_token_refresh_retries_the_link(self):
        link = make_link(saved_quote(), 'apostille')
        cache.delete(TOKEN_CACHE_KEY)
        cache.set(TOKEN_COOLDOWN_KEY, 1, 600)                  # Zoho throttled the refresh a moment ago

        engine.run_link(link.pk)

        link.refresh_from_db()
        self.assertEqual((link.state, link.last_error_code), ('retry', 'TOKEN_RATE_LIMIT'))
        self.assertIsNotNone(link.next_attempt_at)
        self.assertEqual(self.creates('Apostille_Services'), [])
        self.assertEqual(mail.outbox, [])                      # no link_failed alert
        self.assertNoHttp()


class ExpandTests(EngineCase):
    def test_star_skips_suggested_and_done(self):
        quote = saved_quote('v5_success', body_changes=rename_to_test)
        self.handle(quote, ['apostille'])
        self.handle(quote, ['translation'], mode='suggest', caller='rules', trigger='rule:x')

        result = self.handle(quote, ['*'], caller='panel', trigger='create_all')
        self.assertEqual(result.body['results'], [])
        self.assertEqual(result.body['message'], messages.NOTHING_TO_DO)

        result = self.handle(quote, ['*'], caller='panel', trigger='create_all', options={'include_suggested': True})
        self.assertEqual(result.http_status, 202)
        self.assertEqual([(r['service'], r['status']) for r in result.body['results']], [('translation', 'queued')])
        self.assertTrue(result.body['message'].startswith('Creating Translation.'))

    def test_star_refused_on_rel_sources(self):
        record = self.crm.add(GAQL_MODULE, Client_Name='ZZ TEST legacy', GET_A_QUOTE_LEADS='Apostille Service')

        result = self.handle((GAQL_MODULE, record['id']), ['*'], caller='panel')

        self.assertEqual((result.http_status, result.body['detail']), (422, 'star_not_allowed'))
        self.assertEqual(result.body['message'], messages.STAR_NOT_ALLOWED)
        self.assertFalse(ServiceLink.objects.exists())

    def test_notary_alias_expands_requested_notary_lines(self):
        self.assertEqual(registry.expand(['notary'], ['apostille', 'mobile-notary', 'ron']), ['mobile-notary', 'ron'])
        self.assertEqual(registry.expand(['notary'], ['apostille']), ['notary'])
        self.assertEqual(registry.expand(['*'], ['apostille', 'retrieval', 'ron']), ['apostille', 'ron'])
        with self.assertRaises(registry.UnknownService):
            registry.expand(['apostile'], [])

        quote = saved_quote('svc_mobile-notary')
        result = self.handle(quote, ['notary'], mode='preview')
        self.assertEqual([(r['service'], r['status']) for r in result.body['results']], [('mobile-notary', 'preview')])

        legacy = self.crm.add(GAQL_MODULE, Client_Name='ZZ TEST legacy', Client_Email='zz-test+legacy@example.com',
                              GET_A_QUOTE_LEADS='Notary Service')
        result = self.handle((GAQL_MODULE, legacy['id']), ['notary'], mode='preview')
        self.assertEqual(self.one(result)['service'], 'notary')
        self.assertEqual(self.one(result)['payload']['Client_Comments'].splitlines()[1], 'Notary type not given')

        unknown = self.handle(quote, ['apostile'])
        self.assertEqual((unknown.http_status, unknown.body['detail']), (422, 'unknown_service'))

    def test_unrequested_service_adds_manager_line(self):
        quote = saved_quote()

        self.handle(quote, ['embassy'], caller='panel')
        self.handle(quote, ['triple-seal'], caller='panel')   # not a v5 service: no quote line

        line = quote.lines.get(service_key='embassy')
        self.assertEqual((line.source, line.position), ('manager', 2))
        self.assertFalse(QuoteLine.objects.filter(quote=quote, service_key='triple-seal').exists())
        self.assertEqual(ServiceLink.objects.get(service_key='embassy').reason, 'manager')
        self.assertEqual(ServiceLink.objects.get(service_key='embassy').quote_line, line)
        self.handle(quote, ['embassy'], caller='panel')
        self.assertEqual(quote.lines.filter(service_key='embassy').count(), 1)


@override_settings(CRM_CREATE_LIMIT_PER_SOURCE_HOUR=2, CRM_CREATE_LIMIT_GLOBAL_HOUR=3)
class LimitTests(EngineCase):
    def test_create_rate_limit_per_source_and_global(self):
        first = saved_quote()
        self.assertEqual(self.one(self.handle(first, ['apostille']))['status'], 'created')
        self.assertEqual(self.one(self.handle(first, ['embassy']))['status'], 'created')

        refused = self.handle(first, ['translation'])
        self.assertEqual((refused.http_status, refused.body['detail']), (429, 'rate_limited'))
        self.assertEqual(refused.body['message'], messages.RATE_LIMITED)
        (alert,) = mail.outbox
        self.assertIn('rate limit', alert.subject)

        # Previews and `exists` answers are not counted and not refused; the system caller is exempt.
        self.assertEqual(self.handle(first, ['translation'], mode='preview').http_status, 200)
        self.assertEqual(self.one(self.handle(first, ['apostille']))['status'], 'exists')
        system = self.handle(first, ['translation'], caller='system', trigger='intake')
        self.assertEqual(self.one(system)['status'], 'queued')

        second = saved_quote('svc_embassy')
        self.assertEqual(self.one(self.handle(second, ['embassy']))['status'], 'created')   # 3 in all
        refused = self.handle(second, ['translation'])
        self.assertEqual(refused.http_status, 429)                                          # global limit
        self.assertEqual(len(self.creates('Embassy_Legalization')) + len(self.creates('Apostille_Services')), 3)


class InlineTests(EngineCase):
    def test_inline_requires_warm_token(self):
        cache.delete(engine.TOKEN_CACHE_KEY)
        quote = saved_quote()

        with self.captureOnCommitCallbacks() as callbacks:
            result = self.handle(quote, ['apostille'])

        self.assertEqual((result.http_status, self.one(result)['status']), (202, 'queued'))
        self.assertEqual(result.body['state'], 'running')
        self.assertEqual(result.body['poll_url'], f'/api/crm/v1/jobs/{result.request.public_id}/')
        self.assertNoHttp()
        self.assertEqual(len(callbacks), 2)   # the task and the write-back

        self.warm_zoho_token()
        other = saved_quote()
        self.assertEqual(self.one(self.handle(other, ['apostille']))['status'], 'created')

    def test_inline_semaphore_busy_returns_202(self):
        cache.add('crm:inline', 'another-worker', 8)

        result = self.handle(saved_quote(), ['apostille'])

        self.assertEqual((result.http_status, self.one(result)['status']), (202, 'queued'))
        self.assertEqual(ServiceLink.objects.get().state, 'pending')
        self.assertNoHttp()
        self.assertEqual(cache.get('crm:inline'), 'another-worker')   # never released by a non-owner

    def test_per_call_timeout_clipped_and_total_under_6s(self):
        quote = saved_quote()
        delay = 1.3
        seen = []
        started = time.monotonic()

        def slow(call):
            if call.url.startswith('https://www.zohoapis.com/'):
                seen.append((time.monotonic() - started, call.timeout))
                if delay > call.timeout[1]:
                    time.sleep(call.timeout[1])
                    raise requests.Timeout()
                time.sleep(delay)
            return self.crm(call)

        self.http.handler = slow
        with self.captureOnCommitCallbacks() as callbacks:
            result = self.handle(quote, ['apostille'])
        elapsed = time.monotonic() - started

        self.assertLessEqual(elapsed, engine.INLINE_BUDGET_S)
        self.assertLess(result.request.duration_ms, 6000)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[0][1], (1.0, 4.0))
        for offset, (connect, read) in seen:
            self.assertEqual(connect, 1.0)
            self.assertLessEqual(read, min(4.0, engine.INLINE_BUDGET_S - offset - 1.5) + 0.05)
            self.assertLessEqual(offset, engine.INLINE_BUDGET_S - 2.5 + 0.05)   # no call starts below 2.5 s
        # The budget ran out before the create: nothing was written, the task takes the link over.
        self.assertEqual(self.creates('Apostille_Services'), [])
        self.assertEqual((result.http_status, self.one(result)['status']), (202, 'queued'))
        link = ServiceLink.objects.get()
        self.assertEqual((link.state, link.attempts), ('pending', 0))
        self.assertGreaterEqual(len(callbacks), 1)

    @override_settings(ENGINE_INLINE=False)
    def test_inline_off_flag_always_202(self):
        with self.captureOnCommitCallbacks() as callbacks:
            result = self.handle(saved_quote(), ['apostille'])

        self.assertEqual((result.http_status, self.one(result)['status']), (202, 'queued'))
        self.assertNoHttp()
        self.assertTrue(callbacks)


class CreateAgainTests(EngineTransactionTestCase):
    def handle(self, quote, **options):
        return engine.handle(sources.for_quote(quote), ['apostille'], mode='create', trigger='panel', caller='system',
                             actor={}, options={'create_again': True, **options})

    def test_next_seq_under_select_for_update(self):
        quote = saved_quote()
        self.gaql_record(quote)
        make_link(quote, 'apostille', state='created', target_record_id='6686316000000000001')
        locked = []
        original = QuerySet.select_for_update

        def spy(queryset, *args, **kwargs):
            locked.append(queryset.model)
            return original(queryset, *args, **kwargs)

        with patch.object(QuerySet, 'select_for_update', spy), patch('crm.engine._publish'), \
                patch('crm.engine._schedule_writeback'):
            second = self.handle(quote)
            third = self.handle(quote)

        self.assertEqual(locked, [ServiceLink, ServiceLink])
        links = ServiceLink.objects.filter(source_key=f'qr:{quote.pk}').order_by('seq')
        self.assertEqual([(link.seq, link.external_key, link.state) for link in links], [
            (1, f'dcmn:qr:{quote.pk}:apostille', 'created'),
            (2, f'dcmn:qr:{quote.pk}:apostille#2', 'pending'),
            (3, f'dcmn:qr:{quote.pk}:apostille#3', 'pending')])
        self.assertEqual([second.http_status, third.http_status], [202, 202])

    @tag('postgres')
    @skipUnless(connection.vendor == 'postgresql', 'row locks need Postgres (TEST_POSTGRES_URL)')
    def test_create_again_race_returns_in_progress(self):
        quote = saved_quote()
        self.gaql_record(quote)
        first = make_link(quote, 'apostille', state='created', target_record_id='6686316000000000001')
        answers = []

        def racing_click():
            try:
                with patch('crm.engine._publish'):
                    answers.append(self.handle(quote))
            finally:
                connection.close()

        with transaction.atomic():
            list(ServiceLink.objects.select_for_update().filter(pk=first.pk))   # the other click holds the rows
            thread = threading.Thread(target=racing_click)
            thread.start()
            time.sleep(0.5)   # let the racing click reach the lock and wait on it
            make_link(quote, 'apostille', seq=2)                                 # …and takes seq 2
        thread.join(10)

        (result,) = answers
        self.assertEqual((result.http_status, result.body['results'][0]['status']), (409, 'in_progress'))
        self.assertEqual(ServiceLink.objects.filter(source_key=f'qr:{quote.pk}').count(), 2)


class ModeTests(EngineCase):
    @override_settings(CRM_ENGINE_MODE='dry_run')
    def test_dry_run_makes_zero_write_calls(self):
        result = self.handle(saved_quote(), ['apostille', 'i9'])

        self.assertEqual(result.http_status, 200)
        self.assertEqual({r['status'] for r in result.body['results']}, {'dry_run'})
        apostille = result.body['results'][0]
        self.assertEqual(apostille['action'], 'create')
        self.assertEqual(apostille['stage'], 'APOSTILE LEADS FROM GET QUOTE PIPELINE')
        self.assertEqual(apostille['payload']['Status'], 'APOSTILE LEADS FROM GET QUOTE PIPELINE')
        self.assertEqual(self.http.writes, [])
        self.assertFalse(ServiceLink.objects.exists())
        self.assertEqual(Track.objects.count(), 0)
        self.assertIn('Preview only (one-click is in test mode)', result.body['message'])

    def test_suggest_makes_no_record(self):
        quote = saved_quote()

        with self.captureOnCommitCallbacks() as callbacks:
            result = self.handle(quote, ['translation'], mode='suggest', caller='rules', trigger='rule:x')

        self.assertEqual(self.one(result)['status'], 'suggested')
        self.assertEqual(ServiceLink.objects.get().state, 'suggested')
        self.assertNoHttp()
        self.assertEqual(len(callbacks), 1)   # the write-back shows the suggestion
        self.assertEqual(result.body['message'], "Suggested only (from the client's answers). Click Create "
                                                 'Translation to make it.')
        again = self.handle(quote, ['translation'], mode='suggest', caller='rules', trigger='rule:x')
        self.assertEqual(self.one(again)['status'], 'suggested')
        self.assertEqual(ServiceLink.objects.count(), 1)

    def test_preview_returns_payload(self):
        quote = saved_quote()

        result = self.handle(quote, ['apostille'], mode='preview', caller='panel')

        item = self.one(result)
        self.assertEqual((item['status'], item['action']), ('preview', 'create'))
        self.assertEqual(item['payload']['Name'], f'ZZ TEST · Apostille · {quote.full_name} · {quote.reference}')
        self.assertEqual(item['payload'][KEY_FIELD], f'dcmn:qr:{quote.pk}:apostille')
        self.assertEqual((item['dropped'], item['blocked']), ([], []))
        self.assertEqual(self.http.writes, [])
        self.assertFalse(ServiceLink.objects.exists())
        stored = result.request.result['results'][0]
        self.assertEqual(stored['payload']['Name'], {'type': 'str', 'len': len(item['payload']['Name'])})
        self.assertIn('payload_sha256', stored)


class DedupeTests(EngineCase):
    def test_client_request_id_within_2min_returns_same_job(self):
        quote = saved_quote()
        first = self.handle(quote, ['apostille'], caller='panel', client_request_id=f'{GAQL_ID}:apostille')

        second = self.handle(quote, ['apostille'], caller='panel', client_request_id=f'{GAQL_ID}:apostille')

        self.assertEqual(first.body['state'], 'running')
        self.assertEqual(second.http_status, 202)
        self.assertEqual(second.body['job_id'], first.body['job_id'])
        self.assertEqual((second.request.state, second.request.error_code), ('refused', 'duplicate_request'))
        self.assertEqual(ServiceLink.objects.count(), 1)
        self.assertEqual(EngineRequest.objects.count(), 2)

    def test_after_terminal_new_request(self):
        quote = saved_quote()
        first = self.handle(quote, ['apostille'], client_request_id='click-1')
        self.assertEqual(first.request.state, 'done')

        second = self.handle(quote, ['apostille'], client_request_id='click-1')

        self.assertNotEqual(second.body['job_id'], first.body['job_id'])
        self.assertEqual(self.one(second)['status'], 'exists')

        third = self.handle(quote, ['embassy'], caller='panel', client_request_id='click-2')
        EngineRequest.objects.filter(pk=third.request.pk).update(created_at=timezone.now() - timedelta(minutes=3))
        fourth = self.handle(quote, ['embassy'], caller='panel', client_request_id='click-2')
        self.assertNotEqual(fourth.body['job_id'], third.body['job_id'])


class AuditTests(EngineCase):
    def test_every_return_path_updates_engine_request(self):
        quote = saved_quote()
        legacy = self.crm.add(GAQL_MODULE, Client_Name='ZZ TEST legacy', GET_A_QUOTE_LEADS='Apostille Service')
        real = saved_quote('v5_success')
        spam = saved_quote(spam_status='spam')
        self.handle(quote, ['embassy'], caller='panel', client_request_id='dup')
        cases = {
            'disabled': lambda: self.handle(quote, ['apostille']),
            'bad_mode': lambda: self.handle(quote, ['apostille'], mode='delete'),
            'source_not_allowed': lambda: self.handle(('Deals', '6686316000012345678'), ['apostille']),
            'zoho_unavailable': lambda: self.handle(quote, ['apostille']),
            'source_not_found': lambda: self.handle((GAQL_MODULE, '6686316000000000404'), ['apostille']),
            'test_only': lambda: self.handle(real, ['apostille']),
            'star_not_allowed': lambda: self.handle((GAQL_MODULE, legacy['id']), ['*']),
            'unknown_service': lambda: self.handle(quote, ['apostile']),
            'refused_spam': lambda: self.handle(spam, ['apostille']),
            'duplicate_request': lambda: self.handle(quote, ['embassy'], caller='panel', client_request_id='dup'),
            'done': lambda: self.handle(quote, ['apostille']),
            'internal': lambda: self.handle(quote, ['i9']),
        }
        for expected, call in cases.items():
            with self.subTest(path=expected):
                before = EngineRequest.objects.count()
                if expected == 'disabled':
                    with override_settings(CRM_ENGINE_MODE='off'):
                        result = call()
                elif expected == 'zoho_unavailable':
                    cache.set(CIRCUIT_KEY, time.time() + 60, 60)
                    result = call()
                    cache.delete(CIRCUIT_KEY)
                elif expected == 'internal':
                    with patch('crm.engine._one_service', side_effect=RuntimeError('boom')), \
                            self.assertLogs('crm.engine', 'ERROR'), self.assertRaises(RuntimeError):
                        call()
                    result = None
                else:
                    result = call()
                self.assertEqual(EngineRequest.objects.count(), before + 1)
                request = EngineRequest.objects.order_by('-pk').first()
                if result is not None:
                    self.assertEqual(request.pk, result.request.pk)
                self.assertIsNotNone(request.http_status)
                self.assertIsNotNone(request.result)
                self.assertIsNotNone(request.duration_ms)
                self.assertNotEqual(request.state, 'accepted')
                code = request.error_code or request.state
                self.assertEqual(code, expected)
                if request.state in engine.TERMINAL_REQUEST_STATES:
                    self.assertIsNotNone(request.finished_at)


class RetryTests(EngineCase):
    def test_creating_with_expired_lease_goes_retry_and_resolves_by_key(self):
        quote = saved_quote()
        link = make_link(quote, 'apostille', state='creating', attempts=1,
                         lease_until=timezone.now() - timedelta(seconds=5))
        # The worker died after Zoho created the record: it holds our key.
        record = self.crm.add('Apostille_Services', **{KEY_FIELD: link.external_key, 'Name': 'Apostille · x',
                                                      'Status': 'APOSTILE LEADS FROM GET QUOTE PIPELINE'})

        reconcile_service_links()

        link.refresh_from_db()
        self.assertEqual((link.state, link.target_record_id, link.origin), ('adopted', record['id'], 'adopted_key'))
        self.assertEqual(self.creates('Apostille_Services'), [])
        self.assertEqual(link.attempts, 2)
