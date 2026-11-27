"""The Deluge sources in `crm/deluge/` (DESIGN §9.2) and the exact bodies they send."""

import json
import re
from pathlib import Path

from django.urls import reverse

from crm.api_views import API_VERSION
from crm.models import EngineRequest
from crm.tests.support import saved_quote
from crm.tests.test_auth import ApiTestCase
from quotes.models import GAQL_MODULE

DELUGE_DIR = Path(__file__).resolve().parents[1] / 'deluge'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SOURCES = {name: (DELUGE_DIR / f'{name}.dg').read_text(encoding='utf-8')
           for name in ('dcmn_create_service', 'dcmn_open_panel')}
URLS = {'dcmn_create_service': 'https://api.dcmobilenotary.net/api/crm/v1/related-services/',
        'dcmn_open_panel': 'https://api.dcmobilenotary.net/api/crm/v1/panel-links/'}
STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
RANDOM_LOOKING = re.compile(r'^[A-Za-z0-9_\-+/=]{20,}$')


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


def body_keys(source: str) -> set[str]:
    """The keys the function puts into its request `body` map."""
    return set(re.findall(r'body\.put\("(\w+)"', source))


class DelugeTests(ApiTestCase):
    def test_uses_connection_and_v1_url(self):
        for name, source in SOURCES.items():
            with self.subTest(name):
                self.assertIn('connection:"dcmn_api"', source)
                self.assertIn('/api/crm/v1/', source)
                self.assertIn(f'url :"{URLS[name]}"', source)
                self.assertIn('type :POST', source)
                self.assertIn('detailed:true', source)
                self.assertTrue(source.startswith(f'string button.{name}('))
        self.assertEqual(reverse('crm_related_services'), '/api/crm/v1/related-services/')
        self.assertEqual(reverse('crm_panel_links'), '/api/crm/v1/panel-links/')
        self.assertIn(f'body.put("api_version", "{API_VERSION}");', SOURCES['dcmn_create_service'])

    def test_no_secret_no_org_variable(self):
        for name, source in SOURCES.items():
            with self.subTest(name):
                self.assertNotIn('X-DCMN-Key', source)
                self.assertNotIn('x-dcmn-key', source.lower())
                self.assertNotIn('getOrgVariable', source)
                self.assertNotIn('zoho.crm.getOrgVariable', source)
                literals = STRING_LITERAL.findall(source)
                self.assertTrue(literals)
                self.assertEqual([value for value in literals if RANDOM_LOOKING.match(value)], [])

    def test_open_panel_returns_server_message_on_error(self):
        source = SOURCES['dcmn_open_panel']
        opened = source.index('if(code == 200 && txt.startsWith("{"))')
        message = source.index('msg = ifnull(txt.toMap().get("message"), "");')
        fallback = source.index('return "Could not open the DCMN panel (HTTP " + code + ")')
        self.assertLess(opened, message)
        self.assertLess(message, fallback)
        self.assertIn('openUrl(txt.toMap().get("url"), "popup window");', source)

        # What the function reads on an error: a JSON body with a message, for each refusal
        refusals = [
            self.post(reverse('crm_panel_links'), {**fixture('deluge_panel_link'), 'action': 'delete'}),
            self.post(reverse('crm_panel_links'), fixture('deluge_panel_link'), key=''),
            self.post(reverse('crm_panel_links'), {**fixture('deluge_panel_link'),
                                                   'source': {'module': 'Deals', 'id': '1'}}),
        ]
        for response in refusals:
            with self.subTest(response.status_code):
                self.assertNotEqual(response.status_code, 200)
                self.assertTrue(response.content.decode().startswith('{'))
                self.assertTrue(response.json()['message'])

    def test_fixture_bodies_route(self):
        create_keys = body_keys(SOURCES['dcmn_create_service'])
        panel_keys = body_keys(SOURCES['dcmn_open_panel'])
        every = saved_quote(zoho_record_id='6686316000077700001')
        notary = saved_quote('svc_mobile-notary', zoho_record_id='6686316000077700002')
        for quote in (every, notary):
            self.gaql_record(quote)
        cases = [('deluge_apostille', self.quote, ['apostille']), ('deluge_create_all', every, ['apostille']),
                 ('deluge_notary', notary, ['mobile-notary'])]

        for name, quote, services in cases:
            with self.subTest(name):
                body = fixture(name)
                self.assertEqual(set(body), create_keys)
                self.assertEqual(body['client_request_id'], f'{body["source"]["id"]}:{body["services"][0]}')
                body['source']['id'] = quote.zoho_record_id
                body['client_request_id'] = f'{quote.zoho_record_id}:{body["services"][0]}'
                response = self.post(body=body)
                self.assertIn(response.status_code, (200, 202), response.json())
                self.assertEqual([r['service'] for r in response.json()['results']], services)
                request = EngineRequest.objects.get(public_id=response.json()['job_id'])
                self.assertEqual(request.trigger, body['trigger'])

        panel = fixture('deluge_panel_link')
        self.assertEqual(set(panel), panel_keys)
        self.assertEqual(panel['source']['module'], GAQL_MODULE)
        response = self.post(reverse('crm_panel_links'), panel)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertIn('/crm/panel/?t=', response.json()['url'])
