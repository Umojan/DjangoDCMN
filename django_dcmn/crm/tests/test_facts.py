"""Facts (DESIGN §7.2): one normalised view of a v5 quote, a Zoho record or a legacy form order; no HTTP."""

from crm.facts import SOURCE_FIELD_MAPS, from_form_order, from_quote, from_zoho_record
from crm.models import ServiceSignal
from crm.registry import REGISTRY, build
from crm.tests.support import CONTACT_ID, GAQL_ID, EngineTestCase, saved_quote
from quotes.models import GAQL_MODULE


class FactsTests(EngineTestCase):
    def test_from_quote_client_source_quote_and_typed(self):
        quote = saved_quote('v5_success', zoho_attribution_id='6686316000000800001')

        facts = from_quote(quote, quote.lines.get(service_key='apostille'))

        self.assertEqual(facts.client, {'name': 'Maria Fernández', 'email': 'maria@example.com',
                                        'phone_e164': '+12025550147', 'phone_raw': quote.phone_raw,
                                        'contact_id': CONTACT_ID, 'address': None})
        self.assertEqual(facts.attribution_id, '6686316000000800001')
        self.assertEqual(facts.source, {
            'kind': 'quote', 'key': f'qr:{quote.pk}', 'module': GAQL_MODULE, 'id': GAQL_ID,
            'url': f'https://crm.zoho.com/crm/org882204370/tab/CustomModule7/{GAQL_ID}', 'ref_label': quote.reference,
            'is_test': False})
        self.assertEqual((facts.quote['n_services'], facts.quote['services_label']), (2, 'Apostille + Translation'))
        self.assertEqual(facts.line['service_key'], 'apostille')
        self.assertEqual(facts.line['addons'], ['translation'])
        self.assertEqual(facts.typed['doc_kind'], 'fbi_report')
        self.assertEqual(facts.typed['doc_label'], 'FBI background check')
        self.assertEqual(facts.typed['country_name'], 'Spain')
        # The translation add-on carries the languages of the translation line.
        self.assertTrue(facts.typed['translation_addon'])
        self.assertEqual((facts.typed['lang_from'], facts.typed['lang_to']), ('English', 'Spanish'))
        self.assertEqual(facts.typed['channel_label'], 'WhatsApp (opted in)')
        self.assertEqual(facts.typed['when_label'], 'As soon as possible')
        self.assertNoHttp()

    def test_translation_line_facts(self):
        quote = saved_quote('svc_translation')

        facts = from_quote(quote, quote.lines.get())

        self.assertEqual(facts.typed['translation_kind'], 'certified_digital')
        self.assertEqual(facts.typed['pages'], 4)
        self.assertEqual((facts.typed['lang_from'], facts.typed['lang_to']), ('English', 'Portuguese'))
        self.assertNotIn('translation_addon', facts.typed)

    def test_from_zoho_record_reads_the_field_map(self):
        record = {'id': GAQL_ID, 'Client_Name': '  ZZ TEST   Legacy ', 'Client_Email': 'zz-test@example.com',
                  'Client_Phone': '202 555 0147', 'Client_Address_Location': '1 Example St',
                  'Client_Comments': 'Call me', 'Name_of_Client': {'id': CONTACT_ID, 'name': 'x'},
                  'Attribution_Record': {'id': '66863160000008'}, 'Number_Of_Documents': 3,
                  'Document_Type': 'Diploma', 'Country_of_Use': 'Italy'}

        facts = from_zoho_record(GAQL_MODULE, record)

        self.assertEqual(facts.client, {'name': 'ZZ TEST Legacy', 'email': 'zz-test@example.com', 'phone_e164': None,
                                        'phone_raw': '202 555 0147', 'contact_id': CONTACT_ID,
                                        'address': '1 Example St'})
        self.assertEqual(facts.attribution_id, '66863160000008')
        self.assertEqual(facts.source['key'], f'rel:gaql:{GAQL_ID}')
        self.assertEqual(facts.source['ref_label'], 'Get A Quote record')
        self.assertTrue(facts.source['is_test'])
        self.assertEqual(facts.typed, {'doc_label': 'Diploma', 'country_name': 'Italy', 'count': 3,
                                       'message': 'Call me'})
        with self.assertRaises(KeyError):
            from_zoho_record('Leads', {'id': '1'})   # a LATER source module has no field map yet

    def test_from_form_order_ref_label_and_signal_languages(self):
        SOURCE_FIELD_MAPS['Apostille_Services'] = {'client_name': 'Client_Name', 'email': 'Client_Email'}
        self.addCleanup(SOURCE_FIELD_MAPS.pop, 'Apostille_Services')
        signal = ServiceSignal(kind='translation_requested', source_kind='form', source_key='form:apostille:943',
                               service_key='translation', data={'lang_from': 'Spanish', 'lang_to': 'English'})

        facts = from_form_order('apostille', 943, {'id': '1', 'Client_Name': 'ZZ TEST Form'}, signal,
                                module='Apostille_Services')

        self.assertEqual(facts.source['kind'], 'form')
        self.assertEqual(facts.source['key'], 'form:apostille:943')
        self.assertEqual(facts.source['ref_label'], 'Apostille order #943')
        self.assertEqual((facts.typed['lang_from'], facts.typed['lang_to']), ('Spanish', 'English'))

    def test_no_contact_details_line(self):
        facts = from_zoho_record(GAQL_MODULE, {'id': GAQL_ID, 'Client_Name': 'ZZ TEST Nobody'})

        payload = build(REGISTRY['apostille'], facts, source_kind='zoho', key=f'dcmn:rel:gaql:{GAQL_ID}:apostille')

        self.assertIn('No contact details on the source record', payload['Client_Comments'])
        self.assertFalse({'Client_Email', 'Email', 'Phone_Number', 'Client_Contact'} & set(payload))
