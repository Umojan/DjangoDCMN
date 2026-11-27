"""crm.zoho transport: create_once, tags, inline budget, error classes, circuit breaker, timeouts (WP1)."""

from unittest.mock import patch

import requests
from django.core.cache import cache

from crm import contacts, zoho
from crm.testing import FakeResponse, NoNetworkTestCase
from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

MODULE = 'Apostille_Services'
KEY = 'dcmn:qr:57:apostille'
RECORD = {'Name': 'ZZ TEST Apostille', 'Stage': 'New'}


def success(record_id: str, status_code: int = 201, **extra) -> FakeResponse:
    item = {'code': 'SUCCESS', 'status': 'success', 'message': 'record added', 'details': {'id': record_id}, **extra}
    return FakeResponse(status_code, {'data': [item]})


def item_error(code: str, details: dict | None = None, status_code: int = 400) -> FakeResponse:
    item = {'code': code, 'status': 'error', 'message': 'free text from Zoho', 'details': details or {}}
    return FakeResponse(status_code, {'data': [item]})


class ZohoTestCase(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.warm_zoho_token()


class CreateOnceTests(ZohoTestCase):
    def test_duplicate_data_id_from_details(self):
        self.http.queue(item_error('DUPLICATE_DATA', {'api_name': zoho.KEY_FIELD, 'id': '777'}))

        result = zoho.create_once(MODULE, RECORD, KEY)

        self.assertEqual(result, zoho.CreateResult(id='777', created=False))
        self.assertEqual(len(self.http.calls), 1)

    def test_duplicate_data_id_from_duplicate_record(self):
        details = {'api_name': zoho.KEY_FIELD, 'duplicate_record': {'id': '778', 'module': {'api_name': MODULE}}}
        self.http.queue(item_error('DUPLICATE_DATA', details, status_code=202))

        result = zoho.create_once(MODULE, RECORD, KEY)

        self.assertEqual(result, zoho.CreateResult(id='778', created=False))
        self.assertEqual(len(self.http.calls), 1)

    def test_upsert_resolve_when_details_lack_id(self):
        self.http.queue(
            item_error('DUPLICATE_DATA', {'api_name': zoho.KEY_FIELD}),
            success('888', status_code=200, action='update'),
        )

        result = zoho.create_once(MODULE, RECORD, KEY, trigger=['workflow'])

        self.assertEqual(result, zoho.CreateResult(id='888', created=False))
        resolve = self.http.calls[1]
        self.assertEqual((resolve.method, resolve.url), ('POST', f'{zoho.API_BASES["v2"]}/{MODULE}/upsert'))
        self.assertEqual(resolve.json, {
            'data': [{'External_Order_Key': KEY}],
            'duplicate_check_fields': ['External_Order_Key'],
            'trigger': [],
        })

    def test_upsert_resolve_when_zoho_omits_the_field_name(self):
        self.http.queue(item_error('DUPLICATE_DATA'), success('889', status_code=200, action='update'))

        self.assertEqual(zoho.create_once(MODULE, RECORD, KEY), zoho.CreateResult(id='889', created=False))

    def test_duplicate_on_another_field_is_permanent(self):
        self.http.queue(item_error('DUPLICATE_DATA', {'api_name': 'Email', 'id': '779'}))

        with self.assertRaises(zoho.ZohoPermanent) as caught:
            zoho.create_once(MODULE, RECORD, KEY)

        self.assertEqual((caught.exception.code, caught.exception.field), ('DUPLICATE_DATA', 'Email'))
        self.assertEqual(len(self.http.calls), 1)

    def test_search_miss_after_duplicate_never_fails(self):
        def zoho_api(call):
            if call.method == 'GET':
                return FakeResponse(204)  # the search index lags: the key is not found yet
            if call.url.endswith('/upsert'):
                return success('890', status_code=200, action='update')
            return item_error('DUPLICATE_DATA', {'api_name': zoho.KEY_FIELD})

        self.http.handler = zoho_api

        self.assertIsNone(zoho.search_by_key(MODULE, KEY))
        result = zoho.create_once(MODULE, RECORD, KEY)

        self.assertEqual(result, zoho.CreateResult(id='890', created=False))
        self.assertEqual([c.method for c in self.http.calls], ['GET', 'POST', 'POST'])

    def test_always_posts_no_search_first(self):
        self.http.queue(success('5001'))

        result = zoho.create_once(MODULE, RECORD, KEY)

        self.assertEqual(result, zoho.CreateResult(id='5001', created=True))
        self.assertEqual(len(self.http.calls), 1)
        call = self.http.calls[0]
        self.assertEqual((call.method, call.url), ('POST', f'{zoho.API_BASES["v2"]}/{MODULE}'))
        self.assertEqual(call.json['data'], [{**RECORD, 'External_Order_Key': KEY}])

    def test_trigger_is_sent_in_body(self):
        self.http.queue(success('5001'), success('5002'))

        zoho.create_once(MODULE, RECORD, KEY)
        zoho.create_once(MODULE, RECORD, f'{KEY}#2', trigger=('workflow',))

        self.assertEqual(self.http.calls[0].json['trigger'], [])
        self.assertEqual(self.http.calls[1].json['trigger'], ['workflow'])

    def test_update_sends_trigger(self):
        self.http.queue(success('5001', status_code=200))

        zoho.update(MODULE, '5001', {'Stage': 'Won'})

        call = self.http.calls[0]
        self.assertEqual((call.method, call.json), ('PUT', {'data': [{'id': '5001', 'Stage': 'Won'}], 'trigger': []}))

    def test_get_returns_none_when_record_is_gone(self):
        self.http.queue(FakeResponse(204), item_error('INVALID_URL_PATTERN', status_code=404))

        self.assertIsNone(zoho.get(MODULE, '1'))
        self.assertIsNone(zoho.get(MODULE, '2'))

    def test_search_criteria_value_is_escaped(self):
        self.assertEqual(zoho.criteria_value('a(b),c\\d'), 'a\\(b\\)\\,c\\\\d')


class TagTests(ZohoTestCase):
    def test_add_tags_never_creates_tags(self):
        self.http.queue(success('5001', status_code=200))

        self.assertTrue(zoho.add_tags(MODULE, '5001', ['ZZ TEST', 'Quote v3']))

        call = self.http.calls[0]
        self.assertEqual((call.method, call.url), ('POST', f'{zoho.API_BASES["v2"]}/{MODULE}/5001/actions/add_tags'))
        self.assertEqual(call.params, {'tag_names': 'ZZ TEST,Quote v3', 'over_write': 'false'})
        self.assertEqual(call.json, {'trigger': []})

        # A tag Zoho does not know: a warning and False, never a call that creates the tag.
        self.http.queue(item_error('INVALID_DATA', {'api_name': 'tag_names'}))
        with self.assertLogs('crm.zoho', level='WARNING') as logs:
            self.assertFalse(zoho.add_tags(MODULE, '5001', ['Missing tag']))
        self.assertIn('tag_missing', '\n'.join(logs.output))
        self.assertEqual(len(self.http.calls), 2)
        self.assertFalse(any('settings/tags' in c.url for c in self.http.calls))

        self.assertFalse(zoho.add_tags(MODULE, '5001', []))
        self.assertEqual(len(self.http.calls), 2)


@patch('crm.zoho.time')
class BudgetTests(ZohoTestCase):
    def test_timeout_clipped_to_remaining(self, clock):
        clock.monotonic.return_value = 100.0

        self.assertEqual(zoho.budget_timeout(104.0), (1.0, 2.5))
        self.assertEqual(zoho.budget_timeout(110.0), (1.0, 4.0))
        self.assertEqual(zoho.budget_timeout(None), zoho.DEFAULT_TIMEOUT)

    def test_none_below_2_5s(self, clock):
        clock.monotonic.return_value = 100.0

        self.assertIsNone(zoho.budget_timeout(102.4))
        self.assertIsNone(zoho.budget_timeout(99.0))
        self.assertEqual(zoho.budget_timeout(102.5), (1.0, 1.0))


class ErrorClassTests(ZohoTestCase):
    def test_timeout_429_5xx_are_transient(self):
        cases = [
            (requests.Timeout('read timed out'), 'TIMEOUT'),
            (requests.ConnectionError('reset'), 'NETWORK'),
            (FakeResponse(429, {'code': 'TOO_MANY_REQUESTS', 'message': 'limit'}), 'TOO_MANY_REQUESTS'),
            (FakeResponse(500), 'HTTP_500'),
            (FakeResponse(503, {'data': [{'code': 'INTERNAL_ERROR'}]}), 'INTERNAL_ERROR'),
        ]
        for answer, code in cases:
            with self.subTest(code=code):
                self.http.queue(answer)
                with self.assertRaises(zoho.ZohoTransient) as caught, \
                        self.assertLogs('crm.zoho', level='WARNING') as logs:
                    zoho.zoho_call('GET', 'Deals/1', params={'email': 'client@example.com'})
                self.assertIsInstance(caught.exception, ZohoTransientError)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('client@example.com', str(caught.exception) + '\n'.join(logs.output))
                zoho._record_success()  # keep the circuit closed between cases

    def test_invalid_data_is_permanent_with_field_name(self):
        self.http.queue(item_error('INVALID_DATA', {'api_name': 'Email', 'json_path': '$.data[0].Email'}))

        with self.assertRaises(zoho.ZohoPermanent) as caught:
            zoho.create_once(MODULE, RECORD, KEY)

        exc = caught.exception
        self.assertIsInstance(exc, ZohoPermanentError)
        self.assertEqual((exc.code, exc.field, exc.http_status), ('INVALID_DATA', 'Email', 400))
        self.assertIn('Email', str(exc))
        self.assertNotIn('free text from Zoho', str(exc))

    def test_item_error_in_a_2xx_answer_is_permanent(self):
        self.http.queue(item_error('MANDATORY_NOT_FOUND', {'api_name': 'Last_Name'}, status_code=202))

        with self.assertRaises(zoho.ZohoPermanent) as caught:
            zoho.update('Contacts', '11', {'Last_Name': ''})

        self.assertEqual((caught.exception.code, caught.exception.field), ('MANDATORY_NOT_FOUND', 'Last_Name'))

    def test_settings_api_error_keeps_zoho_code(self):
        """Settings-API answers list their results under `fields`, not `data`."""
        item = {'code': 'DUPLICATE_DATA', 'status': 'error', 'message': 'free text', 'details': {'api_name': 'Name'}}
        self.http.queue(FakeResponse(400, {'fields': [item]}))

        with self.assertRaises(zoho.ZohoPermanent) as caught:
            zoho.zoho_call('POST', 'settings/fields', params={'module': MODULE}, json={'fields': [{}]}, api='v6')

        self.assertEqual((caught.exception.code, caught.exception.field), ('DUPLICATE_DATA', 'Name'))

    def test_record_write_without_trigger_is_refused_before_http(self):
        """Invariant 4 at the one transport: a v2 record write that forgot `trigger` never leaves Django."""
        for method in ('POST', 'PUT'):
            with self.subTest(method=method), self.assertRaises(ValueError):
                zoho.zoho_call(method, MODULE, json={'data': [{'Name': 'x'}]})

        self.assertNoHttp()

    def test_permanent_errors_do_not_open_the_circuit(self):
        for _ in range(zoho.CIRCUIT_THRESHOLD + 1):
            self.http.queue(item_error('INVALID_DATA', {'api_name': 'Email'}))
            with self.assertRaises(zoho.ZohoPermanent):
                zoho.zoho_call('POST', MODULE, json={'data': [], 'trigger': []})

        self.assertFalse(zoho.circuit_is_open())


@patch('crm.zoho.time')
class CircuitTests(ZohoTestCase):
    def fail(self, times: int):
        for _ in range(times):
            self.http.queue(FakeResponse(502))
            with self.assertRaises(zoho.ZohoTransient), self.assertLogs('crm.zoho', level='WARNING'):
                zoho.zoho_call('GET', 'Deals/1')

    def test_opens_after_5_transient_in_120s_and_blocks_http(self, clock):
        clock.time.return_value = 1000.0
        self.fail(4)
        self.assertFalse(zoho.circuit_is_open())

        clock.time.return_value = 1110.0  # the fifth error, still within 120 s of the first
        self.fail(1)
        self.assertTrue(zoho.circuit_is_open())

        with self.assertRaises(zoho.CircuitOpen) as caught:
            zoho.zoho_call('GET', 'Deals/1')
        self.assertIsInstance(caught.exception, ZohoTransientError)
        self.assertEqual(len(self.http.calls), 5)

    def test_errors_outside_the_window_or_after_a_success_do_not_open(self, clock):
        clock.time.return_value = 1000.0
        self.fail(4)
        clock.time.return_value = 1130.0  # the first four are older than 120 s
        self.fail(1)
        self.assertFalse(zoho.circuit_is_open())

        self.fail(3)
        self.http.queue(success('1', status_code=200))
        zoho.zoho_call('GET', 'Deals/1')
        self.fail(4)
        self.assertFalse(zoho.circuit_is_open())

    def test_closes_after_60s(self, clock):
        clock.time.return_value = 1000.0
        self.fail(5)

        clock.time.return_value = 1059.0
        self.assertTrue(zoho.circuit_is_open())

        clock.time.return_value = 1060.5
        self.assertFalse(zoho.circuit_is_open())
        self.http.queue(FakeResponse(200, {'data': [{'id': '1'}]}))
        self.assertEqual(zoho.zoho_call('GET', 'Deals/1'), {'data': [{'id': '1'}]})
        self.assertEqual(len(self.http.calls), 6)


class TimeoutTests(ZohoTestCase):
    def test_timeout_tuple_passed_to_requests(self):
        self.http.queue(
            FakeResponse(200, {'data': [{'id': '1'}]}),
            item_error('DUPLICATE_DATA', {'api_name': zoho.KEY_FIELD}),
            success('891', status_code=200, action='update'),
        )

        zoho.get(MODULE, '1')
        zoho.create_once(MODULE, RECORD, KEY, timeout=(1.0, 2.5))

        self.assertEqual([c.timeout for c in self.http.calls], [(5, 15), (1.0, 2.5), (1.0, 2.5)])
        self.assertEqual(self.http.calls[0].headers['Authorization'], 'Zoho-oauthtoken test-token')

    def test_v6_calls_use_the_v6_base(self):
        self.http.queue(FakeResponse(200, {'fields': [{'api_name': 'Stage'}]}))

        self.assertEqual(zoho.fields_metadata(MODULE), [{'api_name': 'Stage'}])
        self.assertTrue(self.http.calls[0].url.startswith(zoho.API_BASES['v6']))


THROTTLED = FakeResponse(200, {'error_description': 'You have made too many requests continuously. '
                                                  'Please try again after some time.',
                                'error': 'Access Denied', 'status': 'failure'})   # live answer, 2026-11-27


class TokenThrottleTests(NoNetworkTestCase):
    """Zoho answers an 11th token request within 10 minutes with HTTP 200 and no access_token."""

    def test_throttled_refresh_is_transient_and_pauses_every_refresh(self):
        self.http.queue(THROTTLED)

        with self.assertRaises(zoho.ZohoTransient) as caught, self.assertLogs('crm.zoho', 'WARNING') as logs:
            zoho.refresh_token((1.0, 4.0))

        self.assertEqual(caught.exception.code, 'TOKEN_RATE_LIMIT')
        self.assertIn('zoho_token_throttled', '\n'.join(logs.output))
        self.assertTrue(zoho.token_cooling_down())
        with self.assertRaises(zoho.TokenCooldown) as again:
            zoho.refresh_token(zoho.LEGACY_TOKEN_TIMEOUT)       # inside the window: no HTTP at all
        self.assertEqual(again.exception.code, 'TOKEN_RATE_LIMIT')
        self.assertEqual(len(self.http.calls), 1)

    def test_background_call_with_a_cold_token_retries_later_and_counts_for_the_circuit(self):
        self.http.queue(THROTTLED)

        with self.assertRaises(zoho.ZohoTransient) as caught, self.assertLogs('crm.zoho', 'WARNING'):
            zoho.zoho_call('GET', 'Deals/1')

        self.assertEqual(caught.exception.code, 'TOKEN_RATE_LIMIT')
        self.assertEqual([c.url for c in self.http.calls], [zoho.TOKEN_URL])
        self.assertEqual(self.http.calls[0].timeout, zoho.LEGACY_TOKEN_TIMEOUT)
        self.assertEqual(len(cache.get(zoho.CIRCUIT_FAILURES_KEY) or []), 1)

    def test_cached_token_keeps_working_during_the_pause(self):
        cache.set(zoho.TOKEN_COOLDOWN_KEY, 1, zoho.TOKEN_COOLDOWN_S)
        self.warm_zoho_token()
        self.http.queue(FakeResponse(200, {'data': [{'id': '1'}]}))

        self.assertEqual(zoho.get(MODULE, '1'), {'id': '1'})
        self.assertEqual(len(self.http.calls), 1)

    def test_refused_refresh_token_stays_permanent(self):
        self.http.queue(FakeResponse(200, {'error': 'invalid_code'}))

        with self.assertRaises(zoho.ZohoPermanent) as caught, self.assertLogs('crm.zoho', 'ERROR'):
            zoho.refresh_token((1.0, 4.0))

        self.assertEqual(caught.exception.code, 'TOKEN_REFRESH')
        self.assertFalse(zoho.token_cooling_down())

    def test_http_error_from_the_token_endpoint_is_transient_without_pause(self):
        self.http.queue(FakeResponse(503))

        with self.assertRaises(zoho.ZohoTransient) as caught:
            zoho.refresh_token((1.0, 4.0))

        self.assertEqual(caught.exception.code, 'TOKEN_REFRESH')
        self.assertFalse(zoho.token_cooling_down())


class UnauthorizedTests(ZohoTestCase):
    def test_scope_mismatch_never_costs_a_token_refresh(self):
        self.http.queue(FakeResponse(401, {'code': 'OAUTH_SCOPE_MISMATCH', 'details': {},
                                           'message': 'invalid oauth scope to access this URL', 'status': 'error'}))

        with self.assertRaises(zoho.ZohoPermanent) as caught:
            zoho.zoho_call('GET', 'org', api='v6')

        self.assertEqual(caught.exception.code, 'OAUTH_SCOPE_MISMATCH')
        self.assertEqual(len(self.http.calls), 1)                   # no refresh, no second GET
        self.assertEqual(cache.get(zoho.TOKEN_CACHE_KEY), 'test-token')

    def test_invalid_token_refreshes_once_and_retries(self):
        self.http.queue(FakeResponse(401, {'code': 'INVALID_TOKEN', 'details': {}, 'message': 'invalid oauth token',
                                           'status': 'error'}),
                        FakeResponse(200, {'access_token': 'fresh-token'}),
                        FakeResponse(200, {'data': [{'id': '1'}]}))

        self.assertEqual(zoho.get(MODULE, '1'), {'id': '1'})

        self.assertEqual([c.url for c in self.http.calls][1], zoho.TOKEN_URL)
        self.assertEqual(self.http.calls[2].headers['Authorization'], 'Zoho-oauthtoken fresh-token')

    def test_unknown_401_code_still_refreshes_once(self):
        self.http.queue(FakeResponse(401, {'code': 'SOME_NEW_AUTH_CODE', 'details': {}, 'status': 'error'}),
                        FakeResponse(200, {'access_token': 'fresh-token'}),
                        FakeResponse(200, {'data': [{'id': '1'}]}))

        self.assertEqual(zoho.get(MODULE, '1'), {'id': '1'})
        self.assertEqual(self.http.calls[1].url, zoho.TOKEN_URL)

    def test_fast_call_drops_only_a_stale_token(self):
        for code, dropped in (('INVALID_TOKEN', True), ('OAUTH_SCOPE_MISMATCH', False)):
            with self.subTest(code=code):
                self.warm_zoho_token()
                self.http.queue(FakeResponse(401, {'code': code, 'details': {}, 'status': 'error'}))
                with self.assertRaises((zoho.ZohoTransient, zoho.ZohoPermanent)), \
                        self.assertLogs('crm.zoho', 'WARNING') if dropped else _no_logs():
                    zoho.zoho_call('GET', 'Deals/1', timeout=(1.0, 2.5))
                self.assertEqual(cache.get(zoho.TOKEN_CACHE_KEY) is None, dropped)
                zoho._record_success()


class _no_logs:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ContactTests(ZohoTestCase):
    def test_no_email_is_skipped_without_http(self):
        self.assertEqual(contacts.find_or_create('Jane Doe', '', '+12025550123'), (None, 'skipped'))
        self.assertEqual(contacts.find_or_create('Jane Doe', 'x' * 95 + '@a.com', ''), (None, 'skipped'))
        self.assertNoHttp()

    def test_existing_contact_is_only_filled_in(self):
        self.http.queue(
            FakeResponse(200, {'data': [{'id': '11', 'Phone': None, 'First_Name': 'Janet'}]}),
            success('11', status_code=200),
        )

        self.assertEqual(contacts.find_or_create('Jane Doe', 'jane@example.com', '+12025550123'), ('11', 'matched'))

        search, fill = self.http.calls
        self.assertEqual((search.method, search.params), ('GET', {'email': 'jane@example.com'}))
        self.assertEqual(fill.json, {'data': [{'id': '11', 'Phone': '+12025550123'}], 'trigger': []})

    def test_missing_contact_is_created_without_triggers(self):
        self.http.queue(FakeResponse(204), success('12'))

        self.assertEqual(contacts.find_or_create('Jane Doe', 'jane@example.com', ''), ('12', 'created'))

        self.assertEqual(self.http.calls[1].json, {
            'data': [{'Last_Name': 'Doe', 'Email': 'jane@example.com', 'First_Name': 'Jane'}],
            'trigger': [],
        })

    def test_parallel_create_resolves_the_duplicate(self):
        self.http.queue(
            FakeResponse(204),
            item_error('DUPLICATE_DATA', {'api_name': 'Email', 'duplicate_record': {'id': '13'}}),
        )

        self.assertEqual(contacts.find_or_create('Jane', 'jane@example.com', None), ('13', 'matched'))
