"""Engine API key auth and caller scopes (DESIGN §8.1)."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm import auth
from crm.alerts import BEAT_HEARTBEAT_KEY
from crm.models import EngineRequest
from crm.tests.support import EngineTestCase, saved_quote
from quotes.models import GAQL_MODULE
from quotes.render import ET

ZOHO_KEY = 'zk-current-6f1c2b9e3d4a4e8b9a512c7d0e5f8a13'
ZOHO_NEXT_KEY = 'zk-next-9b0c3a1f7e2d4c6b8a9f0e1d2c3b4a59'
GMAIL_KEY = 'gm-4e8b9a512c7d0e5f8a136f1c2b9e3d4a'
RULES_KEY = 'rl-7e2d4c6b8a9f0e1d2c3b4a599b0c3a1f'
ACTION_KEYS = f'zoho:{ZOHO_KEY},zoho-next:{ZOHO_NEXT_KEY},gmail:{GMAIL_KEY},rules:{RULES_KEY}'


@override_settings(CRM_ACTION_KEYS=ACTION_KEYS, CRM_ENGINE_TEST_ONLY=True)
class ApiTestCase(EngineTestCase):
    """The engine test set-up plus the four action keys and helpers for the three endpoints."""

    url = reverse('crm_related_services')

    def setUp(self):
        super().setUp()
        cache.set(BEAT_HEARTBEAT_KEY, time.time(), 3600)   # beat is alive: no reconciler_stale email
        self.quote = saved_quote()
        self.gaql_record(self.quote)

    def body(self, services=('apostille',), **changes) -> dict:
        body = {'api_version': 'rs.v1', 'source': {'module': GAQL_MODULE, 'id': self.quote.zoho_record_id},
                'services': list(services), 'mode': 'create', 'trigger': 'zoho_button',
                'actor': {'zoho_user_id': '6686316000000503001', 'email': 'manager@example.com'},
                'client_request_id': f'{self.quote.zoho_record_id}:{services[0]}'}
        body.update(changes)
        return body

    def post(self, url=None, body=None, *, key=ZOHO_KEY, **extra):
        headers = {'HTTP_X_DCMN_KEY': key} if key else {}
        return self.client.post(url or self.url, data=json.dumps(self.body() if body is None else body),
                                content_type='application/json', **headers, **extra)

    def get(self, url, *, key=ZOHO_KEY, **params):
        headers = {'HTTP_X_DCMN_KEY': key} if key else {}
        return self.client.get(url, params, **headers)


class KeyTests(ApiTestCase):
    def test_missing_key_401(self):
        response = self.post(key='')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['detail'], 'unauthorized')
        self.assertTrue(response.json()['message'])
        self.assertEqual(response['WWW-Authenticate'], 'X-DCMN-Key')
        self.assertEqual(self.zoho_writes(), [])

    def test_wrong_key_401(self):
        with self.assertLogs('crm.auth', 'WARNING') as logs:
            response = self.post(key='zk-wrong-00000000000000000000000000')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['detail'], 'unauthorized')
        self.assertIn('crm_key_refused reason=wrong_key', '\n'.join(logs.output))
        self.assertFalse(EngineRequest.objects.filter(state='done').exists())

    def test_rotated_next_key_accepted(self):
        response = self.post(key=ZOHO_NEXT_KEY)

        self.assertEqual(response.status_code, 200, response.json())
        request = EngineRequest.objects.get(public_id=response.json()['job_id'])
        self.assertEqual((request.caller, request.caller_key_id),
                         ('zoho', hashlib.sha256(ZOHO_NEXT_KEY.encode()).hexdigest()[:8]))
        with override_settings(CRM_ACTION_KEYS=f'zoho:{ZOHO_NEXT_KEY}'):   # step 5: the old key is gone
            self.assertEqual(self.post(key=ZOHO_KEY).status_code, 401)

    def test_compare_digest_called_for_every_key(self):
        with patch('crm.auth.hmac.compare_digest', wraps=hmac.compare_digest) as spy:
            self.assertEqual(self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id,
                                      key=ZOHO_KEY).status_code, 200)

        self.assertEqual(spy.call_count, 4)   # the first key matched; the other three were still compared
        compared = [call.args[1] for call in spy.call_args_list]
        self.assertEqual(compared, [ZOHO_KEY.encode(), ZOHO_NEXT_KEY.encode(), GMAIL_KEY.encode(),
                                    RULES_KEY.encode()])

    def test_body_or_query_token_ignored_and_counted(self):
        body = {**self.body(), 'key': ZOHO_KEY, 'token': ZOHO_KEY}
        with self.assertLogs('crm.auth', 'WARNING') as logs:
            response = self.post(body=body, key='')
            query = self.client.get(f'{self.url}?module={GAQL_MODULE}&id={self.quote.zoho_record_id}'
                                    f'&api_key={ZOHO_KEY}')

        self.assertEqual((response.status_code, query.status_code), (401, 401))
        self.assertEqual(auth.ignored_tokens_on(timezone.now().astimezone(ET).date()), 2)
        output = '\n'.join(logs.output)
        self.assertIn('body_token_ignored fields=key,token', output)
        self.assertIn('body_token_ignored fields=api_key', output)
        self.assertNotIn(ZOHO_KEY, output)

    def test_key_never_in_logs(self):
        with self.assertLogs(level='DEBUG') as logs:
            self.post()
            self.post(key=ZOHO_KEY[:-1] + 'x')
            self.post(reverse('crm_panel_links'), {'source': {'module': GAQL_MODULE, 'id': '1'}, 'action': 'x'})
        output = '\n'.join(logs.output)
        for key in (ZOHO_KEY, ZOHO_NEXT_KEY, GMAIL_KEY, RULES_KEY, ZOHO_KEY[:-1] + 'x'):
            self.assertNotIn(key, output)
        stored = json.dumps(list(EngineRequest.objects.values('result', 'actor', 'caller_key_id', 'error_code')))
        self.assertNotIn(ZOHO_KEY, stored)

    def test_caller_key_id_is_sha256_prefix(self):
        response = self.post()

        request = EngineRequest.objects.get(public_id=response.json()['job_id'])
        self.assertEqual(request.caller_key_id, hashlib.sha256(ZOHO_KEY.encode()).hexdigest()[:8])
        self.assertEqual(auth.key_id(ZOHO_KEY.encode()), request.caller_key_id)
        self.assertEqual(auth.parse_keys('zoho:k,zoho-next:k2, gmail : g ,broken,:x'),
                         [('zoho', b'k'), ('zoho', b'k2'), ('gmail', b'g')])


class ScopeTests(ApiTestCase):
    def test_gmail_cannot_post_related_services(self):
        response = self.post(key=GMAIL_KEY)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'scope')
        self.assertTrue(response.json()['message'])
        refused = EngineRequest.objects.get()
        self.assertEqual((refused.caller, refused.state, refused.error_code), ('gmail', 'refused', 'scope'))
        self.assertEqual(self.zoho_writes(), [])
        status = self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id, key=GMAIL_KEY)
        self.assertEqual(status.status_code, 200)   # gmail may read the lines

    def test_rules_only_events(self):
        job = self.post().json()['job_id']
        calls = [
            self.post(key=RULES_KEY),
            self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id, key=RULES_KEY),
            self.get(reverse('crm_job', args=[job]), key=RULES_KEY),
            self.post(reverse('crm_panel_links'), {'source': {'module': GAQL_MODULE, 'id': self.quote.zoho_record_id},
                                                   'action': 'add_service'}, key=RULES_KEY),
        ]

        self.assertEqual([response.status_code for response in calls], [403, 403, 403, 403])
        self.assertTrue(all(response.json()['detail'] == 'scope' for response in calls))
        self.assertEqual(EngineRequest.objects.filter(caller='rules', error_code='scope').count(), 4)
