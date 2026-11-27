"""S1 shadow mapping of legacy quotes: GET requests only, receiver only with the flag (DESIGN §13.2, WP4)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.core.cache import cache
from django.db.models.signals import post_save
from django.test import override_settings

import quotes as quotes_package
from crm import schema
from crm.schema_spec import GAQL
from crm.testing import FakeResponse
from crm.tests.test_schema_command import FakeZoho
from orders.models import QuoteRequest, ZohoSyncJob
from quotes import shadow
from quotes.models import ShadowRun
from quotes.tasks import shadow_map_legacy_quote
from quotes.tests.test_pipeline import PipelineTestCase

TIMELINES = Path(__file__).resolve().parents[2] / 'crm' / 'tests' / 'fixtures' / 'timeline'
GAQL_ID = '6686316000033717016'       # the copy fixture: GAQL "Quote ID2763" → Apostille 6686316000033736004
COPY_ID = '6686316000033736004'
DISPATCH_UID = 'quotes_shadow_legacy_quote'


def legacy_quote(**fields) -> QuoteRequest:
    """A legacy QuoteRequest without its post_save receivers (bulk_create), as if the legacy sync ran."""
    values = {'name': 'Ana Legacy', 'email': 'ana.legacy@example.com', 'phone': '(202) 555-0177', 'number': '1',
              'appointment_date': '2026-11-27', 'appointment_time': '10:00', 'services': 'Apostille Service',
              'comments': 'FBI report for Spain'}
    values.update(fields)
    QuoteRequest.objects.bulk_create([QuoteRequest(**values)])
    return QuoteRequest.objects.latest('pk')


def seed_module_schema(module: str) -> None:
    """The module's metadata after ZOHO-SETUP Z3, as the guard and adoption read it."""
    fake = FakeZoho()
    fake.create_api_fields()
    cache.set(schema.CACHE_KEY.format(module=module), schema.by_api_name(schema.normalise(fake.fields[module])),
              schema.CACHE_TTL_S)


def receiver_connected() -> bool:
    return any(key[0] == DISPATCH_UID for key, *_ in post_save.receivers)


class ShadowTests(PipelineTestCase):
    def test_builds_shadow_run_with_get_requests_only(self):
        """Adoption steps 3 and 4 of crm.adoption, read-only, on the recorded copy pair (fixture 7 pairs, A23)."""
        quote_request = legacy_quote()
        ZohoSyncJob.objects.create(order_type='quote', order_id=quote_request.pk, zoho_module=GAQL,
                                   zoho_record_id=GAQL_ID)
        self.crm.records[(GAQL, GAQL_ID)] = {
            'id': GAQL_ID, 'Name': 'Quote ID2763', 'GET_A_QUOTE_LEADS': 'Apostille Service',
            'Client_Name': quote_request.name, 'Client_Email': quote_request.email,
            'Client_Phone': quote_request.phone, 'Client_Comments': quote_request.comments}
        for module, record_id in ((GAQL, GAQL_ID), ('Apostille_Services', COPY_ID)):
            self.crm.timelines[record_id] = json.loads(
                (TIMELINES / f'facts-timeline-{module}-{record_id}.json').read_text(encoding='utf-8'))
        copy = {'id': COPY_ID, 'Name': 'Apostille Service', 'External_Order_Key': None,
                'Status': 'APOSTILE LEADS FROM GET QUOTE PIPELINE', 'Created_Time': '2026-11-26T15:19:11+06:00'}
        self.crm.fail[('GET', 'Apostille_Services/search')] = FakeResponse(200, {'data': [copy]})
        seed_module_schema('Apostille_Services')

        with override_settings(QUOTE_V3_SHADOW_LEGACY=True):
            self.assertEqual(shadow_map_legacy_quote.apply((quote_request.pk,)).get(), 'done')

        run = ShadowRun.objects.get()
        self.assertEqual((run.quote_request_id, run.gaql_record_id, run.error), (quote_request.pk, GAQL_ID, ''))
        self.assertEqual(run.timeline_copy, {'module': 'Apostille_Services', 'id': COPY_ID, 'copies': 1})
        self.assertEqual(run.heuristic_copy, {'module': 'Apostille_Services', 'id': COPY_ID, 'matches': 1,
                                              'by': 'email'})
        self.assertTrue(run.agree)
        self.assertEqual([w for w in run.schema_warnings if w.startswith('blocked:')], [])
        self.assertEqual(run.payload_preview['payload']['GET_A_QUOTE_LEADS'], {'type': 'str', 'len': 17})
        target = run.payload_preview['target']
        self.assertEqual(target['module'], 'Apostille_Services')
        self.assertIn('External_Order_Key', target['payload'])
        self.assertRegex(target['payload_sha256'], r'^[0-9a-f]{64}$')
        stored = json.dumps([run.payload_preview, run.timeline_copy, run.heuristic_copy, run.schema_warnings])
        for value in ('Ana Legacy', 'ana.legacy@example.com', '555-0177', 'FBI report'):
            self.assertNotIn(value, stored)

        search = next(call for call in self.http.calls if call.url.endswith('Apostille_Services/search'))
        self.assertIn('(Client_Email:equals:ana.legacy@example.com)', search.params['criteria'])
        self.assertTrue(any(call.url.endswith(f'Apostille_Services/{COPY_ID}/__timeline')
                            for call in self.http.calls))   # the copy-rule proof of step 4
        self.assertEqual({call.method for call in self.http.calls}, {'GET'})
        self.assertEqual(self.http.writes, [])

    def test_heuristic_without_copy_rule_proof_does_not_agree(self):
        """A record that only looks like the copy (no copy rule on its timeline) is never named by step 4."""
        quote_request = legacy_quote()
        ZohoSyncJob.objects.create(order_type='quote', order_id=quote_request.pk, zoho_module=GAQL,
                                   zoho_record_id=GAQL_ID)
        self.crm.records[(GAQL, GAQL_ID)] = {'id': GAQL_ID, 'GET_A_QUOTE_LEADS': 'Apostille Service',
                                             'Client_Name': quote_request.name, 'Client_Email': quote_request.email}
        look_alike = {'id': '6686316000033799999', 'Name': 'Apostille Service', 'External_Order_Key': None,
                      'Status': 'APOSTILE LEADS FROM GET QUOTE PIPELINE', 'Created_Time': '2026-11-26T15:19:11+06:00'}
        self.crm.fail[('GET', 'Apostille_Services/search')] = FakeResponse(200, {'data': [look_alike]})
        seed_module_schema('Apostille_Services')

        run = shadow.shadow_map(quote_request.pk)

        self.assertIsNone(run.timeline_copy)        # an empty GAQL timeline
        self.assertEqual(run.heuristic_copy, {'module': 'Apostille_Services', 'id': '', 'matches': 1, 'by': 'email'})
        self.assertIsNone(run.agree)
        self.assertEqual(self.http.writes, [])

    def test_receiver_not_connected_when_flag_off(self):
        self.assertFalse(receiver_connected())
        config = apps.get_app_config('quotes')
        with override_settings(QUOTE_V3_SHADOW_LEGACY=False):
            config.ready()
        self.assertFalse(receiver_connected())
        self.assertEqual(shadow_map_legacy_quote.apply((1,)).get(), 'skipped')
        self.assertFalse(ShadowRun.objects.exists())

        with patch.dict(sys.modules):
            sys.modules.pop('quotes.signals', None)
            vars(quotes_package).pop('signals', None)       # `from . import signals` must import it afresh
            self.addCleanup(vars(quotes_package).pop, 'signals', None)
            with override_settings(QUOTE_V3_SHADOW_LEGACY=True):
                config.ready()
            self.addCleanup(post_save.disconnect, sender=QuoteRequest, dispatch_uid=DISPATCH_UID)
            self.assertTrue(receiver_connected())

            receiver = sys.modules['quotes.signals'].shadow_legacy_quote
            with patch.object(shadow_map_legacy_quote, 'apply_async') as publish, \
                    self.captureOnCommitCallbacks(execute=True):
                receiver(sender=QuoteRequest, instance=QuoteRequest(pk=77), created=True)
                receiver(sender=QuoteRequest, instance=QuoteRequest(pk=78), created=False)
        publish.assert_called_once_with((77,), countdown=90)

    def test_not_copying_bucket_has_no_copy_and_no_verdict(self):
        quote_request = legacy_quote(services='Pre-Check Document Review')
        ZohoSyncJob.objects.create(order_type='quote', order_id=quote_request.pk, zoho_module=GAQL,
                                   zoho_record_id='6686316000033717999')

        run = shadow.shadow_map(quote_request.pk)

        self.assertEqual((run.timeline_copy, run.heuristic_copy, run.agree, run.error), (None, None, None, ''))
        self.assertNoHttp()
