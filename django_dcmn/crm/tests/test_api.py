"""Engine HTTP API (DESIGN §8.2, §8.3): related-services, jobs, panel-links."""

import json
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from crm import messages
from crm.api_views import PANEL_OPENING_MESSAGE
from crm.models import EngineRequest, ServiceLink
from crm.panel.tokens import read_panel_token
from crm.tasks.links import run_service_link
from crm.tests.support import make_link, saved_quote
from crm.tests.test_auth import RULES_KEY, ApiTestCase
from crm.zoho import CIRCUIT_KEY
from quotes.models import GAQL_MODULE

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def open_circuit() -> None:
    cache.set(CIRCUIT_KEY, time.time() + 60, 60)


class RelatedServicesTests(ApiTestCase):
    def test_request_and_200_response_shape(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {'ok', 'job_id', 'ref', 'state', 'message', 'open_url', 'results', 'warnings'})
        (result,) = body['results']
        link = ServiceLink.objects.get()
        self.assertEqual(result, {'service': 'apostille', 'status': 'created', 'module': 'Apostille_Services',
                                  'id': link.target_record_id, 'url': link.target_url,
                                  'tracking_id': link.tracking_id or None, 'link_id': link.pk,
                                  'created_at': result['created_at'], 'trigger': 'zoho_button'})
        self.assertEqual((body['ok'], body['state'], body['open_url'], body['warnings']),
                         (True, 'done', link.target_url, []))
        self.assertEqual(body['message'], 'Apostille created in Apostille Services. Opening it.')
        request = EngineRequest.objects.get(public_id=body['job_id'])
        self.assertEqual((request.caller, request.trigger, request.mode, request.client_request_id, request.ref),
                         ('zoho', 'zoho_button', 'create', f'{self.quote.zoho_record_id}:apostille', body['ref']))
        self.assertEqual(request.actor, {'kind': 'zoho_user', 'zoho_user_id': '6686316000000503001',
                                         'email': 'manager@example.com'})

    @override_settings(ENGINE_INLINE=False)
    def test_202_shape_with_poll_url(self):
        with self.captureOnCommitCallbacks() as callbacks:
            response = self.post()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual((body['ok'], body['state'], body['open_url']), (True, 'running', None))
        self.assertEqual(body['poll_url'], f'/api/crm/v1/jobs/{body["job_id"]}/')
        (result,) = body['results']
        self.assertEqual((result['service'], result['status'], result['link_id']),
                         ('apostille', 'queued', ServiceLink.objects.get().pk))
        self.assertTrue(body['message'])
        self.assertEqual(self.zoho_writes(), [])
        self.assertTrue(callbacks)   # the link task runs after the commit

    def test_options_ignored_from_zoho_caller_with_warning(self):
        body = self.body(options={'force_spam': True, 'overrides': {'owner_id': '6686316000000999999'}})

        with self.assertLogs('crm.api_views', 'WARNING'):
            response = self.post(body=body)

        self.assertEqual(response.status_code, 200)
        self.assertIn('options_ignored: options are accepted from the DCMN panels only', response.json()['warnings'])
        (post,) = self.creates('Apostille_Services')
        self.assertNotIn('Owner', post.json['data'][0])
        stored = EngineRequest.objects.get(public_id=response.json()['job_id']).result
        self.assertIn('options_ignored: options are accepted from the DCMN panels only', stored['warnings'])

    def test_422_unknown_service(self):
        response = self.post(body=self.body(services=['teleport']))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail'], 'unknown_service')
        self.assertTrue(response.json()['message'])
        self.assertEqual(self.zoho_writes(), [])

    def test_409_in_progress(self):
        make_link(self.quote, 'apostille', state='creating', lease_until=timezone.now() + timedelta(seconds=30))

        response = self.post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['results'][0]['status'], 'in_progress')
        self.assertEqual(response.json()['message'], messages.IN_PROGRESS)

    def test_503_circuit_open_message(self):
        open_circuit()

        response = self.post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'zoho_unavailable')
        self.assertEqual(response.json()['message'], messages.ZOHO_UNAVAILABLE)
        self.assertNoHttp()

    def test_every_body_has_message(self):
        responses = {
            '200': self.post(),
            '400': self.post(body={'api_version': 'rs.v1'}),
            '400 not json': self.client.post(self.url, data='{', content_type='application/json',
                                             HTTP_X_DCMN_KEY=self.zoho_key),
            '401': self.post(key=''),
            '403 scope': self.post(key=RULES_KEY),
            '404 job': self.get(reverse('crm_job', args=[uuid.uuid4()])),
            '405': self.client.put(self.url, data='{}', content_type='application/json',
                                   HTTP_X_DCMN_KEY=self.zoho_key),
            '422': self.post(body=self.body(services=['teleport'])),
        }
        with override_settings(CRM_ENGINE_MODE='off'):
            responses['503 disabled'] = self.post()
            responses['503 disabled GET'] = self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id)
        real = saved_quote('v5_success', zoho_record_id='6686316000044444444')
        self.gaql_record(real)
        responses['403 test_only'] = self.post(body={**self.body(), 'source': {'module': GAQL_MODULE,
                                                                               'id': real.zoho_record_id}})
        responses['202'] = self.post(body=self.body(services=['apostille', 'embassy'], client_request_id='two'))

        for name, response in responses.items():
            with self.subTest(name):
                self.assertTrue(response.json().get('message'), response.json())
        self.assertEqual(responses['202'].status_code, 202)
        self.assertEqual(responses['403 test_only'].json()['detail'], 'test_only')

    def test_open_url_only_for_single_record(self):
        many = self.post(body=self.body(services=['apostille', 'embassy']))

        self.assertEqual(many.status_code, 202, many.json())
        self.assertIsNone(many.json()['open_url'])
        for link in ServiceLink.objects.all():
            run_service_link(link.pk)
        done = self.get(reverse('crm_job', args=[many.json()['job_id']])).json()
        self.assertEqual([r['status'] for r in done['results']], ['created', 'created'])
        self.assertIsNone(done['open_url'])   # two records: nothing to open
        one = self.post(body=self.body(services=['apostille'], client_request_id='again'))
        self.assertEqual(one.json()['results'][0]['status'], 'exists')
        self.assertEqual(one.json()['open_url'], one.json()['results'][0]['url'])

    @property
    def zoho_key(self) -> str:
        from crm.tests.test_auth import ZOHO_KEY

        return ZOHO_KEY


class StatusTests(ApiTestCase):
    def test_get_lines_summary(self):
        self.post()

        response = self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        link = ServiceLink.objects.get()
        self.assertEqual(body['source'], {'kind': 'quote', 'key': f'qr:{self.quote.pk}',
                                          'ref_label': self.quote.reference})
        (line,) = body['lines']
        self.assertEqual({k: v for k, v in line.items() if k != 'done_by'}, {
            'service': 'apostille', 'label': 'Apostille', 'requested': True, 'state': 'created',
            'target_url': link.target_url, 'candidates': [], 'link_id': link.pk})
        self.assertRegex(line['done_by'], r'^button · \d{1,2} \w{3} \d{2}:\d{2}$')
        self.assertEqual(body['message'], '1 of 1 services are in Zoho.')
        writes = len(self.zoho_writes())
        self.get(self.url, module=GAQL_MODULE, id=self.quote.zoho_record_id)
        self.assertEqual(len(self.zoho_writes()), writes)   # the GET writes nothing
        self.assertEqual(self.get(self.url, module=GAQL_MODULE, id='abc').status_code, 400)


class JobTests(ApiTestCase):
    def test_job_poll_states(self):
        with override_settings(ENGINE_INLINE=False):
            job_id = self.post().json()['job_id']
        url = reverse('crm_job', args=[job_id])

        running = self.get(url).json()
        self.assertEqual((running['ok'], running['state'], running['job_id']), (True, 'running', job_id))
        self.assertEqual(running['results'][0]['status'], 'queued')
        self.assertIsNone(running['open_url'])

        run_service_link(ServiceLink.objects.get().pk)
        done = self.get(url).json()
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['results'][0]['status'], 'created')
        self.assertEqual(done['open_url'], ServiceLink.objects.get().target_url)
        self.assertTrue(done['message'])

        missing = self.get(reverse('crm_job', args=[uuid.uuid4()]))
        self.assertEqual((missing.status_code, missing.json()['detail']), (404, 'not_found'))
        self.assertTrue(missing.json()['message'])


class PanelLinkTests(ApiTestCase):
    url = reverse('crm_panel_links')

    def link_body(self, **changes) -> dict:
        return {**json.loads((FIXTURES / 'deluge_panel_link.json').read_text()),
                'source': {'module': GAQL_MODULE, 'id': self.quote.zoho_record_id}, **changes}

    def test_url_token_ttl_900_and_single_use(self):
        response = self.post(body=self.link_body())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body['ok'], body['expires_in'], body['message']), (True, 900, PANEL_OPENING_MESSAGE))
        prefix = f'{settings.BASE_URL}/crm/panel/?t='
        self.assertTrue(body['url'].startswith(prefix))
        token = body['url'][len(prefix):]
        self.assertNotIn('manager@example.com', token)
        stored = EngineRequest.objects.get(trigger='panel_link')
        self.assertEqual((stored.caller, stored.mode, stored.state, stored.source_key),
                         ('zoho', 'panel', 'done', f'qr:{self.quote.pk}'))
        self.assertNotIn(token, json.dumps(stored.result))

        later = time.time() + 901
        with patch('django.core.signing.time.time', return_value=later), self.assertRaises(Exception) as expired:
            read_panel_token(token)
        self.assertEqual(type(expired.exception).__name__, 'SignatureExpired')

        first = self.client.get(f'/crm/panel/?t={token}')
        again = self.client.get(f'/crm/panel/?t={token}')
        self.assertEqual((first.status_code, again.status_code), (200, 403))

    def test_panel_link_only_add_service(self):
        response = self.post(body=self.link_body(action='create_service'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'bad_action')
        self.assertTrue(response.json()['message'])
        refused = EngineRequest.objects.get()
        self.assertEqual((refused.state, refused.error_code, refused.trigger), ('refused', 'bad_action', 'panel_link'))
        self.assertNoHttp()

    def test_panel_link_test_only_refuses_real_record(self):
        real = saved_quote('v5_success', zoho_record_id='6686316000044444444')
        self.gaql_record(real)

        response = self.post(body=self.link_body(source={'module': GAQL_MODULE, 'id': real.zoho_record_id}))

        self.assertEqual(response.status_code, 403)
        self.assertEqual((response.json()['detail'], response.json()['message']), ('test_only', messages.TEST_ONLY))
        self.assertNotIn('url', response.json())
        with override_settings(CRM_ENGINE_TEST_ONLY=False):
            self.assertEqual(self.post(body=self.link_body(source={'module': GAQL_MODULE,
                                                                   'id': real.zoho_record_id})).status_code, 200)

    @override_settings(CRM_PANEL_LINK_ALERT_HOUR=2)
    def test_panel_link_spike_alert(self):
        statuses = [self.post(body=self.link_body()).status_code for _ in range(4)]

        self.assertEqual(statuses, [200, 200, 200, 200])   # links are still minted
        (alert,) = mail.outbox
        self.assertIn('panel links', alert.subject)
        self.assertIn('threshold 2', alert.body)
        self.assertNotIn('manager@example.com', alert.body)
        self.assertNotIn(self.quote.full_name, alert.body)

    def test_error_bodies_carry_message(self):
        responses = {
            '400 body': self.post(body={'action': 'add_service'}),
            '400 action': self.post(body=self.link_body(action='delete')),
            '401': self.post(body=self.link_body(), key=''),
            '403 scope': self.post(body=self.link_body(), key=RULES_KEY),
            '403 module': self.post(body=self.link_body(source={'module': 'Deals', 'id': '1'})),
            '404 source': self.post(body=self.link_body(source={'module': GAQL_MODULE, 'id': '6686316000000000001'})),
        }
        with override_settings(CRM_ENGINE_MODE='off'):
            responses['503 disabled'] = self.post(body=self.link_body())
        open_circuit()
        responses['503 circuit'] = self.post(body=self.link_body())

        expected = {'400 body': 400, '400 action': 400, '401': 401, '403 scope': 403, '403 module': 403,
                    '404 source': 404, '503 disabled': 503, '503 circuit': 503}
        for name, response in responses.items():
            with self.subTest(name):
                self.assertEqual(response.status_code, expected[name])
                self.assertTrue(response.json().get('message'))
                self.assertFalse(response.json()['ok'])
