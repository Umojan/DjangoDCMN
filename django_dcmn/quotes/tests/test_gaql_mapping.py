"""The GAQL record of a v3 quote (DESIGN §5.3): golden payload, bucket, lengths, guard, spam tag (WP4)."""

import copy
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from crm import schema
from crm.schema_spec import GAQL
from crm.testing import FakeResponse
from crm.tests.test_zoho import item_error
from quotes import gaql, pipeline
from quotes.models import QuoteSubmission
from quotes.tests.test_contract import fixture_names, load_fixture, make_body
from quotes.tests.test_pipeline import PipelineTestCase, seed_gaql_schema, v5_quote

GOLDEN = Path(__file__).resolve().parent / 'golden' / 'gaql_v5_success.json'
CONTACT_ID = '6686316000000000101'
ATTRIBUTION_ID = '6686316000000000202'


def accepted_bodies() -> dict[str, dict]:
    """Every contract fixture the endpoint accepts (201), with a fresh submission_id."""
    bodies = {}
    for name in fixture_names():
        fixture = load_fixture(name)
        if fixture['expect'].get('status') == 201:
            bodies[name] = {**copy.deepcopy(fixture['body']), 'submission_id': str(uuid.uuid4())}
    return bodies


def string_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in string_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in string_values(item)]
    return []


class PayloadTests(PipelineTestCase):
    def test_v5_success_payload_equals_golden(self):
        quote = v5_quote(reference='Q-482913', zoho_contact_id=CONTACT_ID, zoho_attribution_id=ATTRIBUTION_ID)
        golden = json.loads(GOLDEN.read_text(encoding='utf-8'))
        golden['External_Order_Key'] = golden['External_Order_Key'].format(pk=quote.pk)

        self.assertEqual(gaql.build_gaql_record(quote), golden)

        guarded = gaql.guarded_record(quote)
        self.assertEqual((guarded.payload, guarded.dropped, guarded.blocked), (golden, [], []))

    def test_bucket_is_web_quote_request_never_a_copying_value(self):
        bodies = accepted_bodies()
        self.assertIn('svc_i9', bodies)
        for name, body in bodies.items():
            with self.subTest(fixture=name):
                quote = v5_quote(body)
                record = gaql.guarded_record(quote).payload
                self.assertEqual(record['GET_A_QUOTE_LEADS'], 'Web Quote Request')
                self.assertFalse(set(string_values(record)) & gaql.COPYING_VALUES)

    def test_quote_status_new_only_on_create(self):
        quote = v5_quote()
        QuoteSubmission.objects.filter(pk=quote.pk).update(amendments=[
            {'at': '2026-11-26T15:00:00Z', 'changed': ['message'], 'payload': {'contact': {'message': 'later'}}}])

        self.assertEqual(pipeline.run(quote.pk), 'synced')
        pipeline.reconcile()

        with_status = [call for call in self.http.writes if 'Quote_Status' in json.dumps(call.json)]
        self.assertEqual([(call.method, call.url.rsplit('/', 1)[1]) for call in with_status], [('POST', GAQL)])
        self.assertEqual(with_status[0].json['data'][0]['Quote_Status'], 'New')
        self.assertEqual([call for call in self.http.writes if call.method == 'PUT' and GAQL in call.url], [])

    def test_number_of_documents_absent_when_unanswered(self):
        body = make_body()
        del body['services'][0]['answers']['count']

        record = gaql.build_gaql_record(v5_quote(body))

        self.assertNotIn('Number_Of_Documents', record)
        self.assertEqual(record['Document_Type'], 'FBI background check')
        self.assertNotIn('1 document', record['Quote_Lines_Text'])

    def test_lengths_respected(self):
        long_email = f'{"m" * 95}@example.com'
        quote = v5_quote(full_name='María ' + 'Fernández' * 10, phone_e164='',
                         phone_raw='+44 (0) 20 7946 0958 ext. 12345 6',
                         email=long_email, email_normalized=long_email, message='word ' * 1000,
                         page_url='https://www.dcmobilenotary.com/schedule-an-appointment?' + 'x=1&' * 200)

        record = gaql.build_gaql_record(quote)
        guarded = gaql.guarded_record(quote)

        self.assertLessEqual(len(record['Name']), 120)
        self.assertTrue(record['Name'].startswith(f'{quote.reference} · María '))
        self.assertEqual(record['Client_Phone'], quote.phone_raw[:30])
        self.assertNotIn('Client_Email', record)
        self.assertLessEqual(len(record['Client_Comments']), 2000)
        self.assertLessEqual(len(record['Quote_Source_Page']), 255)
        self.assertEqual(guarded.blocked, [])
        fields = schema.get_fields(GAQL)
        for name, value in guarded.payload.items():
            limit = fields[name].get('length')
            if isinstance(value, str) and limit:
                with self.subTest(field=name):
                    self.assertLessEqual(len(value), limit)

    def test_missing_web_quote_request_value_blocks(self):
        seed_gaql_schema(web_quote_request=False)
        quote = v5_quote()

        result = gaql.guarded_record(quote)
        self.assertEqual(result.blocked, ['GET_A_QUOTE_LEADS'])
        self.assertNotIn('GET_A_QUOTE_LEADS', result.payload)

        self.assertEqual(pipeline.run(quote.pk), 'failed')

        self.assertEqual(self.calls('POST', GAQL), [])        # never created, never with another bucket
        quote.refresh_from_db()
        self.assertEqual(quote.sync_last_error, 'record: SCHEMA_BLOCKED (GET_A_QUOTE_LEADS)')
        self.assertEqual(len(self.ops_alerts('blocking field missing')), 1)
        self.assertEqual(len(self.ops_alerts('not synced')), 1)

    def test_suspect_adds_possible_spam_tag_via_crm_zoho(self):
        quote = v5_quote(spam_status='suspect', spam_reasons=['turnstile_failed'])

        with patch('orders.zoho_sync.add_tags_to_record') as legacy:
            pipeline.step_record(quote)

        legacy.assert_not_called()
        quote.refresh_from_db()
        (tag,) = self.calls('POST', f'{GAQL}/{quote.zoho_record_id}/actions/add_tags')
        self.assertEqual(tag.params, {'tag_names': 'possible-spam', 'over_write': 'false'})
        self.assertEqual(tag.json, {'trigger': []})

        clean = v5_quote()
        pipeline.step_record(clean)
        self.assertEqual(len([c for c in self.http.calls if c.url.endswith('/actions/add_tags')]), 1)

        missing_tag = v5_quote(spam_status='suspect')
        self.crm.fail[('POST', 'Get_A_Quote_Leads/6686316000090000003/actions/add_tags')] = item_error('INVALID_DATA')
        pipeline.step_record(missing_tag)              # a missing tag is a warning, the record stands
        missing_tag.refresh_from_db()
        self.assertEqual(missing_tag.zoho_record_id, '6686316000090000003')

    def test_contact_preference_is_effective_channel_omitted_when_null(self):
        quote = v5_quote(preferred_channel='whatsapp', effective_channel='', whatsapp_opt_in=False)
        self.assertNotIn('Contact_Preference', gaql.build_gaql_record(quote))

        for channel, display in (('call', 'Call'), ('text', 'Text'), ('whatsapp', 'WhatsApp'), ('email', 'Email')):
            QuoteSubmission.objects.filter(pk=quote.pk).update(effective_channel=channel)
            quote.refresh_from_db()
            with self.subTest(channel=channel):
                self.assertEqual(gaql.build_gaql_record(quote)['Contact_Preference'], display)

    def test_subform_rows_only_when_auto_and_field_present(self):
        quote = v5_quote()
        self.assertFalse(gaql.use_subform())
        self.assertNotIn('Quote_Lines', gaql.build_gaql_record(quote))

        rows = gaql.build_gaql_record(quote, subform=True)['Quote_Lines']
        self.assertEqual([(r['Service_Key'], r['Line_Ref'], r['Line_Status']) for r in rows],
                         [('apostille', f'{quote.reference}/1', 'Requested'),
                          ('translation', f'{quote.reference}/2', 'Requested')])
        self.assertEqual(rows[1]['Price_Shown'], 'Price after free review')

    def test_transient_tag_error_does_not_fail_record(self):
        quote = v5_quote(spam_status='suspect')
        self.crm.fail[('POST', 'Get_A_Quote_Leads/6686316000090000001/actions/add_tags')] = FakeResponse(503, {})

        pipeline.step_record(quote)

        quote.refresh_from_db()
        self.assertEqual(quote.zoho_record_id, '6686316000090000001')
