"""Registry of related services (DESIGN §7.4): values against the dumps, enum coverage, stages, URLs, golden payloads."""

import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.test import override_settings

from crm import registry, schema, tid
from crm.facts import Facts, from_quote, from_zoho_record
from crm.models import ServiceLink
from crm.registry import (
    AUTOMATED_MARK, CLOSED_STAGE_PATTERN, REGISTRY, TRANSLATION_TYPES, build, enum_rendering, record_name, stage_for,
)
from crm.testing import NoNetworkTestCase
from crm.tests.support import CONTACT_ID, GAQL_ID, GOLDEN_DIR, engine_settings, saved_quote, seed_metadata
from crm.tests.test_schema_command import baseline_fields
from quotes import contract as c
from quotes.models import GAQL_MODULE

FROZEN_NOW = datetime(2026, 11, 26, 15, 0, tzinfo=dt_timezone.utc)
GOLDEN_REFERENCE = 'Q-482913'
GOLDEN_PK = 57
ATTRIBUTION_ID = '6686316000000800001'

# Golden facts per registry key: a contract fixture and its line, or a legacy GAQL record (rel:gaql source).
GOLDEN_SOURCES = {
    'apostille': ('v5_success', 'apostille'), 'embassy': ('svc_embassy', 'embassy'),
    'translation': ('svc_translation', 'translation'), 'fbi': ('svc_fbi', 'fbi'),
    'fingerprinting': ('svc_fingerprinting', 'fingerprinting'), 'mobile-notary': ('svc_mobile-notary', 'mobile-notary'),
    'office-notary': ('svc_office-notary', 'office-notary'), 'ron': ('svc_ron', 'ron'), 'i9': ('svc_i9', 'i9'),
    'notary': ('record', 'Notary Service'), 'triple-seal': ('record', 'Apostille Service'),
    'fbi-apostille-deal': ('v5_success', 'apostille'),
}
LEGACY_GAQL = {
    'id': GAQL_ID, 'Client_Name': 'ZZ TEST legacy client', 'Client_Email': 'zz-test+legacy@example.com',
    'Client_Phone': '(202) 555-0147', 'Client_Address_Location': '1 Example Street NW, Washington, DC 20001',
    'Client_Comments': 'Marriage certificate for Italy, please call after 5.', 'Name_of_Client': {'id': CONTACT_ID},
    'Attribution_Record': {'id': ATTRIBUTION_ID}, 'Number_Of_Documents': 2,
    'Document_Type': 'Marriage certificate', 'Country_of_Use': 'Italy',
}


def metadata(*, api_fields: bool = True) -> dict[str, dict]:
    """Normalised metadata by module: the baseline dumps, optionally after ZOHO-SETUP Z3 (`create_api_fields`)."""
    if not api_fields:
        return {module: schema.by_api_name(schema.normalise(baseline_fields(module)))
                for module in schema.REFRESH_MODULES}
    fake = seed_metadata()
    return {module: schema.by_api_name(schema.normalise(raw)) for module, raw in fake.fields.items()}


def make_facts(*, line: dict | None = None, kind: str = 'quote', key: str = 'qr:1', **typed) -> Facts:
    return Facts(
        client={'name': 'ZZ TEST Jane Doe', 'email': 'jane.doe@example.com', 'phone_e164': '+12025550123',
                'phone_raw': '(202) 555-0123', 'contact_id': None, 'address': None},
        attribution_id=None,
        source={'kind': kind, 'key': key, 'module': GAQL_MODULE, 'id': GAQL_ID, 'url': None, 'ref_label': 'Q-100001',
                'is_test': True},
        quote={'reference': 'Q-100001', 'n_services': 1} if kind == 'quote' else None,
        line=line, typed=typed,
    )


def golden_payload(key: str) -> dict:
    """The build() output of one registry key from its fixed golden facts (DB access; date frozen)."""
    fixture, detail = GOLDEN_SOURCES[key]
    spec = REGISTRY[key]
    if fixture == 'record':
        facts = from_zoho_record(GAQL_MODULE, {**LEGACY_GAQL, 'GET_A_QUOTE_LEADS': detail})
        source_kind, external_key = 'zoho', f'dcmn:rel:gaql:{GAQL_ID}:{key}'
    else:
        quote = saved_quote(fixture, zoho_attribution_id=ATTRIBUTION_ID)
        facts = from_quote(quote, quote.lines.get(service_key=detail))
        facts.source.update(key=f'qr:{GOLDEN_PK}', ref_label=GOLDEN_REFERENCE)
        facts.quote.update(pk=GOLDEN_PK, reference=GOLDEN_REFERENCE)
        source_kind, external_key = 'quote', f'dcmn:qr:{GOLDEN_PK}:{key}'
    with patch('crm.registry.timezone.now', return_value=FROZEN_NOW):
        return build(spec, facts, source_kind=source_kind, key=external_key)


@engine_settings
class RegistryTestCase(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.metadata = metadata()


class ValuesTests(RegistryTestCase):
    def test_every_stage_and_constant_is_active_display_value_in_baseline(self):
        baseline = metadata(api_fields=False)
        for spec in REGISTRY.values():
            fields = baseline[spec.module]
            for stage in spec.stages():
                with self.subTest(service=spec.key, stage=stage):
                    self.assertIn(stage, fields[spec.stage_field]['pick_list_values'])
            for name, value in spec.constants.items():
                with self.subTest(service=spec.key, constant=name):
                    self.assertIn(name, fields)
                    if fields[name]['data_type'] == 'picklist':
                        self.assertIn(value, fields[name]['pick_list_values'])
            for name, values in spec.typed_values.items():
                if name not in fields:
                    continue   # a §6 field created by `zoho_schema --apply`; checked by ValidateTests
                for value in values:
                    with self.subTest(service=spec.key, field=name, value=value):
                        self.assertIn(value, fields[name]['pick_list_values'])

    def test_no_automated_by_crm_stage(self):
        for spec in REGISTRY.values():
            for stage in spec.stages():
                with self.subTest(service=spec.key):
                    self.assertNotIn(AUTOMATED_MARK, stage)

    def test_closed_pattern_matches_no_intake_value(self):
        for spec in REGISTRY.values():
            for stage in spec.stages():
                with self.subTest(service=spec.key, stage=stage):
                    self.assertIsNone(CLOSED_STAGE_PATTERN.search(stage))
        # …and it does catch the closed stages of the dumps.
        for closed in ('Client Lost ❌ (silent or chose another provider)', 'Cancelled', 'Rejected',
                       'Completed ✅ (Send Review) (Automation Email)', 'No Review/ Closed Lost', 'Shipping/drop off',
                       'Useless Leads ( Wrong Numbers, Blogers, etc)', 'Delivered / Submitted'):
            with self.subTest(closed=closed):
                self.assertIsNotNone(CLOSED_STAGE_PATTERN.search(closed))

    def test_translation_type_mapping(self):
        active = self.metadata['Translation_Services']['Translation_Type']['pick_list_values']
        self.assertEqual(TRANSLATION_TYPES['certified_digital'], 'Digital Certified')
        self.assertEqual(TRANSLATION_TYPES['notarized_wet'], 'Certified Notarized')
        spec = REGISTRY['translation']
        for kind in c.ANSWER_LABELS[('translation', 'kind')]:
            with self.subTest(kind=kind):
                payload = build(spec, make_facts(translation_kind=kind, lang_from='English', lang_to='Spanish'),
                                source_kind='quote', key='dcmn:qr:1:translation')
                comments = payload['Client_Comments']
                if TRANSLATION_TYPES[kind]:
                    self.assertIn(payload['Translation_Type'], active)
                    self.assertNotIn('no Zoho option', comments)
                else:
                    self.assertNotIn('Translation_Type', payload)
                    label = c.ANSWER_LABELS[('translation', 'kind')][kind]
                    self.assertIn(f'Translation type: {label} (no Zoho option)', comments)
                self.assertEqual(payload['Languages'], 'English → Spanish')

    def test_fingerprint_display_value_used(self):
        raw = next(f for f in baseline_fields('FINGERPRINT_SERVICES') if f['api_name'] == 'Fingerprint_Stage')
        option = next(o for o in raw['pick_list_values']
                      if o['display_value'] == 'FINGERPRINT LEADS FROM GET QUOTE PIPELINE')
        self.assertNotEqual(option['actual_value'], option['display_value'])   # the live dump: they differ
        for key in ('fbi', 'fingerprinting'):
            with self.subTest(service=key):
                payload = build(REGISTRY[key], make_facts(), source_kind='quote', key=f'dcmn:qr:1:{key}')
                self.assertEqual(payload['Fingerprint_Stage'], option['display_value'])


class EnumCoverageTests(RegistryTestCase):
    def test_every_schema_enum_value_has_mapping_or_explicit_omit(self):
        schema_json = json.loads(c.SCHEMA_PATH.read_text(encoding='utf-8'))
        checked = 0
        for rule in schema_json['$defs']['serviceLine']['allOf']:
            service = rule['if']['properties']['service']['const']
            answers = rule['then']['properties']['answers'].get('properties', {})
            for answer, definition in answers.items():
                for value in definition.get('enum') or []:
                    with self.subTest(service=service, answer=answer, value=value):
                        rendering = enum_rendering(service, answer, value)
                        self.assertIsNotNone(rendering, 'no mapping, no explicit omit, no comment rendering')
                        self.assertIn(rendering[0], ('field', 'omit', 'comment'))
                        if rendering[0] == 'field':
                            _, api_name, target = rendering
                            spec = REGISTRY[service]
                            self.assertIn(api_name, spec.typed_fields)
                            meta = self.metadata[spec.module][api_name]
                            if meta['data_type'] == 'picklist':
                                self.assertIn(target, meta['pick_list_values'])
                        checked += 1
        self.assertGreater(checked, 60)

    def test_embassy_corporate_maps_company_documents(self):
        payload = build(REGISTRY['embassy'], make_facts(embassy_kind='corporate', country_name='United Arab Emirates'),
                        source_kind='quote', key='dcmn:qr:1:embassy')
        self.assertEqual(payload['Document_Type'], 'Company documents')
        self.assertEqual(payload['Country_of_Legalization'], 'United Arab Emirates')
        self.assertIs(payload['File_Uploaded'], False)

    def test_apostille_state_maps_service_area(self):
        expected = {'DC': 'DC (District of Columbia)', 'MD': 'MD (Maryland)', 'VA': 'VA (Virginia',
                    'NY': 'Outside DMV Area service', 'CA': 'Outside DMV Area service', 'unsure': None}
        active = self.metadata['Apostille_Services']['Service_Area']['pick_list_values']
        for state, area in expected.items():
            with self.subTest(state=state):
                payload = build(REGISTRY['apostille'], make_facts(state=state), source_kind='quote',
                                key='dcmn:qr:1:apostille')
                self.assertEqual(payload.get('Service_Area'), area)
                if area:
                    self.assertIn(area, active)
        payload = build(REGISTRY['apostille'], make_facts(), source_kind='quote', key='dcmn:qr:1:apostille')
        self.assertNotIn('Service_Area', payload)   # a federal document: no state asked, nothing written


class DeadMappingTests(RegistryTestCase):
    def test_no_fingerprint_address_no_notary_count_no_office_as_address(self):
        facts = make_facts(people=2, count=3, zip='20001', office='arlington',
                           line={'answers': {'office': 'arlington', 'where': 'mobile'}})
        facts.client['address'] = '1 Example Street NW'
        for key in ('fbi', 'fingerprinting'):
            with self.subTest(service=key):
                payload = build(REGISTRY[key], facts, source_kind='quote', key=f'dcmn:qr:1:{key}')
                self.assertNotIn('Service_Address_for_mobile', payload)
                self.assertFalse({'Address', 'Client_Address', 'Client_Address_Location'} & set(payload))
        for key in ('mobile-notary', 'office-notary', 'ron', 'notary'):
            with self.subTest(service=key):
                payload = build(REGISTRY[key], facts, source_kind='quote', key=f'dcmn:qr:1:{key}')
                self.assertNotIn('Number_of_Documents', payload)
        for key in ('office-notary', 'ron', 'notary'):
            with self.subTest(service=key):
                payload = build(REGISTRY[key], facts, source_kind='quote', key=f'dcmn:qr:1:{key}')
                self.assertNotIn('Client_Address_Location', payload)

    def test_office_zip_when_in_comments(self):
        office = build(REGISTRY['office-notary'], make_facts(office='dc', line={'answers': {'office': 'dc'}}),
                       source_kind='quote', key='dcmn:qr:1:office-notary')
        self.assertIn('Preferred office: Washington, DC', office['Client_Comments'])

        mobile = build(REGISTRY['mobile-notary'], make_facts(zip='20001', line={'answers': {'when': 'this_week'}}),
                       source_kind='quote', key='dcmn:qr:1:mobile-notary')
        self.assertEqual(mobile['Client_Address_Location'], 'ZIP 20001')
        self.assertIn('When: this week', mobile['Client_Comments'])

        invalid = build(REGISTRY['mobile-notary'], make_facts(zip_raw='2000-1abcdefghij',
                                                              line={'answers': {'when': 'today'}}),
                        source_kind='quote', key='dcmn:qr:1:mobile-notary')
        self.assertNotIn('Client_Address_Location', invalid)
        self.assertIn('ZIP as typed (not valid): 2000-1abcd', invalid['Client_Comments'])
        self.assertIn('When: today', invalid['Client_Comments'])


class StageTests(RegistryTestCase):
    def test_triple_seal_non_form_source_uses_from_apostille_stage(self):
        spec = REGISTRY['triple-seal']
        cases = {('quote', 'qr:57'): 'TRIPLE SEAL ORDERS FROM APOSTILLE',
                 ('zoho', f'rel:gaql:{GAQL_ID}'): 'TRIPLE SEAL ORDERS FROM APOSTILLE',
                 ('zoho', 'rel:deal:6686316000012345678'): 'TRIPLE SEAL ORDERS FROM APOSTILLE',
                 ('zoho', 'rel:apo:6686316000012345678'): 'TRIPLE SEAL ORDERS FROM APOSTILLE',
                 ('form', 'form:apostille:943'): 'TRIPLE SEAL ORDERS FROM APOSTILLE',
                 ('form', 'form:marriage:12'): 'Order Received'}
        for (kind, key), stage in cases.items():
            with self.subTest(source=key):
                self.assertEqual(stage_for(spec, make_facts(kind=kind, key=key), kind), stage)

    def test_fbi_apostille_deal_uses_from_apostille_request_in_standard(self):
        spec = REGISTRY['fbi-apostille-deal']
        for kind, key in (('quote', 'qr:57'), ('zoho', f'rel:gaql:{GAQL_ID}'), ('form', 'form:fbi:77')):
            with self.subTest(source=key):
                payload = build(spec, make_facts(kind=kind, key=key), source_kind=kind, key=f'dcmn:{key}:deal')
                self.assertEqual(payload['Stage'], 'FROM APOSTILLE REQUEST')
                self.assertEqual(payload['Pipeline'], 'Standard (Standard)')
                self.assertNotEqual(payload['Stage'], 'Order Received')
        self.assertIn('FROM APOSTILLE REQUEST', self.metadata['Deals']['Stage']['pick_list_values'])
        # The apostille itself, made from a Deal, goes to the federal requests stage.
        apostille = stage_for(REGISTRY['apostille'], make_facts(kind='zoho', key='rel:deal:1'), 'zoho')
        self.assertEqual(apostille, 'FBI - Federal Apostille Requests')


class UrlTests(RegistryTestCase):
    def test_tab_per_module_deals_is_potentials(self):
        expected = {'Apostille_Services': 'CustomModule2', 'Embassy_Legalization': 'CustomModule1',
                    'Translation_Services': 'CustomModule3', 'FINGERPRINT_SERVICES': 'CustomModule10',
                    'Notary_Services': 'CustomModule6', 'I_9_Verification': 'CustomModule5',
                    'Triple_Seal_Apostilles': 'CustomModule4', 'Deals': 'Potentials'}
        for spec in REGISTRY.values():
            with self.subTest(service=spec.key):
                self.assertEqual(spec.tab, expected[spec.module])
                self.assertEqual(spec.url('6686316000033736004'),
                                 f'https://crm.zoho.com/crm/org882204370/tab/{expected[spec.module]}/6686316000033736004')
        self.assertEqual(REGISTRY['fbi-apostille-deal'].url('1'), 'https://crm.zoho.com/crm/org882204370/tab/Potentials/1')


class GoldenTests(RegistryTestCase):
    def test_payload_per_service_equals_golden(self):
        self.assertEqual(set(GOLDEN_SOURCES), set(REGISTRY))
        for key in REGISTRY:
            with self.subTest(service=key):
                payload = golden_payload(key)
                golden = json.loads((GOLDEN_DIR / f'{key}.json').read_text(encoding='utf-8'))
                self.assertEqual(payload, golden)
                # Every golden field exists in Zoho (after ZOHO-SETUP) and passes the guard unchanged.
                guarded = schema.guard(REGISTRY[key].module, payload, REGISTRY[key].blocking)
                self.assertEqual((guarded.dropped, guarded.blocked), ([], []))
                self.assertEqual(guarded.payload, payload)

    def test_name_format_and_zz_test_prefix(self):
        spec = REGISTRY['apostille']
        facts = make_facts()
        facts.client['name'] = 'Maria Fernández'
        facts.source.update(ref_label='Q-482913', is_test=False)
        self.assertEqual(record_name(spec, facts), 'Apostille · Maria Fernández · Q-482913')

        facts.client['name'] = 'ZZ TEST Jane Doe'
        facts.source['is_test'] = True
        self.assertEqual(record_name(spec, facts), 'ZZ TEST · Apostille · ZZ TEST Jane Doe · Q-482913')

        facts.client['name'] = 'ZZ TEST ' + 'Very Long Name ' * 20
        name = record_name(spec, facts)
        self.assertLessEqual(len(name), registry.NAME_MAX)
        self.assertTrue(name.startswith('ZZ TEST · Apostille · ZZ TEST Very Long'))
        self.assertTrue(name.endswith('… · Q-482913'))

        facts.client['name'] = ''
        self.assertEqual(record_name(spec, facts), 'ZZ TEST · Apostille · Q-482913')

        deal = build(REGISTRY['fbi-apostille-deal'], make_facts(), source_kind='quote', key='dcmn:qr:1:deal')
        self.assertEqual(deal['Deal_Name'], 'ZZ TEST · FBI Apostille · ZZ TEST Jane Doe · Q-100001')
        self.assertNotIn('Name', deal)

    def test_triple_seal_has_no_quote_request_and_no_tid(self):
        spec = REGISTRY['triple-seal']
        payload = golden_payload('triple-seal')
        self.assertNotIn('Quote_Request', payload)
        self.assertNotIn('Tracking_ID', payload)
        self.assertIsNone(spec.tid_service)
        self.assertNotIn('Tracking_ID', self.metadata['Triple_Seal_Apostilles'])
        with override_settings(ENGINE_TID_POLICY={'Triple_Seal_Apostilles': 'mint_silent'}):
            link = ServiceLink(source_kind='quote', source_key='qr:1', service_key=spec.key)
            self.assertIsNone(tid.mint_silent(spec.module, spec.key, make_facts(), link))


class ValidateTests(RegistryTestCase):
    def test_drift_returns_problems(self):
        self.assertEqual(registry.validate(self.metadata), [])

        drifted = {module: {name: dict(meta) for name, meta in fields.items()}
                   for module, fields in self.metadata.items()}
        del drifted['Apostille_Services']['Country_of_Use']
        drifted['Notary_Services']['Notary_Stages']['pick_list_values'] = [
            v for v in drifted['Notary_Services']['Notary_Stages']['pick_list_values']
            if v != 'NOTARY LEADS FROM GET QUOTE PIPELINE']
        drifted['Translation_Services']['Pages']['data_type'] = 'text'
        drifted['Deals']['Pipeline']['pick_list_values'] = ['FBI Apostille Pipeline']

        problems = registry.validate(drifted)

        self.assertIn('Apostille_Services.Country_of_Use: field missing (apostille)', problems)
        self.assertIn("Notary_Services.Notary_Stages: value 'NOTARY LEADS FROM GET QUOTE PIPELINE' not active "
                      "(mobile-notary)", problems)
        self.assertIn('Translation_Services.Pages: type text, expected integer (translation)', problems)
        self.assertIn("Deals.Pipeline: value 'Standard (Standard)' not active (fbi-apostille-deal)", problems)
        # Only the modules present in the metadata are checked.
        self.assertEqual(registry.validate({}), [])
        self.assertEqual(schema.validate_registry(drifted), problems)
