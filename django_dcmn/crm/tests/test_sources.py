"""Canonical sources (DESIGN §7.2): qr / form / rel keys, requested services, the allow-list."""

from unittest.mock import patch

from django.test import override_settings

from crm import sources
from crm.facts import SOURCE_FIELD_MAPS
from crm.models import ServiceSignal
from crm.sources import SourceNotAllowed, SourceNotFound
from crm.tests.support import GAQL_ID, EngineTestCase, saved_quote
from crm.zoho import KEY_FIELD
from quotes.models import GAQL_MODULE


class ResolveTests(EngineTestCase):
    def test_qr_key_resolves_to_quote(self):
        quote = saved_quote('v5_success')
        self.gaql_record(quote)

        source = sources.resolve(GAQL_MODULE, GAQL_ID)

        self.assertEqual((source.kind, source.key, source.module, source.record_id),
                         ('quote', f'qr:{quote.pk}', GAQL_MODULE, GAQL_ID))
        self.assertEqual(source.requested, ['apostille', 'translation'])
        self.assertEqual(source.ref_label, quote.reference)
        self.assertEqual(source.quote, quote)
        self.assertEqual(source.record[KEY_FIELD], f'dcmn:qr:{quote.pk}')
        self.assertEqual(len(self.http.calls), 1)   # one GET, Django's own token

    def test_form_key_resolves_to_form_source(self):
        record = self.crm.add('Apostille_Services', **{KEY_FIELD: 'dcmn:apostille:943', 'Client_Name': 'ZZ TEST'})
        ServiceSignal.objects.create(dedupe_key='translation_requested:form:apostille:943:translation',
                                     kind='translation_requested', source_kind='form',
                                     source_key='form:apostille:943', service_key='translation')

        with override_settings(CRM_SOURCES_ALLOWED=[GAQL_MODULE, 'Apostille_Services']), \
                patch.dict(SOURCE_FIELD_MAPS, {'Apostille_Services': {'client_name': 'Client_Name'}}):
            source = sources.resolve('Apostille_Services', record['id'])
            facts = sources.facts_for(source, 'translation')

        self.assertEqual((source.kind, source.key), ('form', 'form:apostille:943'))
        self.assertEqual(source.form_order, ('apostille', 943))
        self.assertEqual(source.requested, ['translation'])
        self.assertEqual(source.ref_label, 'Apostille order #943')
        self.assertEqual(facts.source['ref_label'], 'Apostille order #943')

    def test_legacy_gaql_bucket_maps_requested_service(self):
        cases = {'Apostille Service': ['apostille'], 'Notary Service': ['notary'],
                 'Translation Service': ['translation'], 'Embassy Legalization Services': ['embassy'],
                 'I-9 Employment Verification Services': ['i9'], 'Fingerprinting Services': ['fingerprinting'],
                 'Web Quote Request': [], 'Phone Call Leads': []}
        for bucket, requested in cases.items():
            with self.subTest(bucket=bucket):
                record = self.crm.add(GAQL_MODULE, **{KEY_FIELD: 'dcmn:quote:12', 'GET_A_QUOTE_LEADS': bucket})
                source = sources.resolve(GAQL_MODULE, record['id'])
                self.assertEqual((source.kind, source.key), ('zoho', f'rel:gaql:{record["id"]}'))
                self.assertEqual(source.requested, requested)
                self.assertEqual(source.ref_label, 'Get A Quote record')
        # A record without any key, and a qr key whose quote is not in Django, are legacy sources too.
        bare = self.crm.add(GAQL_MODULE, GET_A_QUOTE_LEADS='Apostille Service')
        self.assertEqual(sources.resolve(GAQL_MODULE, bare['id']).key, f'rel:gaql:{bare["id"]}')
        orphan = self.crm.add(GAQL_MODULE, **{KEY_FIELD: 'dcmn:qr:999999'})
        self.assertEqual(sources.resolve(GAQL_MODULE, orphan['id']).kind, 'zoho')

    def test_module_not_allowed_raises(self):
        with self.assertRaises(SourceNotAllowed):
            sources.resolve('Deals', '6686316000012345678')
        with override_settings(CRM_SOURCES_ALLOWED=[GAQL_MODULE, 'Leads']), self.assertRaises(SourceNotAllowed):
            sources.resolve('Leads', '6686316000012345678')    # allowed, but no SOURCE_FIELD_MAPS row yet
        with override_settings(CRM_SOURCES_ALLOWED=[]), self.assertRaises(SourceNotAllowed):
            sources.resolve(GAQL_MODULE, GAQL_ID)
        self.assertNoHttp()
        with self.assertRaises(SourceNotFound):
            sources.resolve(GAQL_MODULE, '6686316000000000404')
