"""Zoho schema (WP2): gating, apply, verify, run log, runtime guard, metadata cache and the spec itself."""

import json
import sys
import types
from unittest.mock import call, patch

import requests
from django.core import mail
from django.core.cache import cache
from django.test import override_settings

from crm import schema, schema_spec
from crm.models import ZohoSchemaSnapshot
from crm.schema_spec import GAQL
from crm.tasks import refresh_zoho_schema
from crm.testing import ZOHO_TEST_TOKEN
from orders.zoho_errors import ZohoTransientError

from .test_schema_command import FIELDS_URL, REFRESHED_TOKEN, SchemaTestCase

SERVICE_DISPLAY = ['Apostille', 'Embassy Legalization', 'Translation', 'FBI Background Check', 'Document Retrieval',
                   'Fingerprinting', 'Mobile Notary', 'Online Notary (RON)', 'Office Notary', 'I-9 Verification',
                   'Other / Not Sure']
DCMN_TOOLTIP = {'name': 'info_icon', 'value': 'Set by DCMN; edits overwritten'}  # non-text fields

# ZOHO-SETUP Z3.1, verbatim (the Services Requested values are the DESIGN §5.3 list).
Z3_EXAMPLES = {
    (GAQL, 'Quote Reference'): {
        'field_label': 'Quote Reference', 'data_type': 'text', 'length': 20,
        'tooltip': {'name': 'static_text', 'value': 'DCMN web quote number'}},
    (GAQL, 'Quote Status'): {
        'field_label': 'Quote Status', 'data_type': 'picklist',
        'pick_list_values': [{'display_value': v, 'actual_value': v}
                             for v in ('New', 'Contacted', 'Quoted', 'Accepted', 'Lost', 'Spam')]},
    (GAQL, 'Services Requested'): {
        'field_label': 'Services Requested', 'data_type': 'multiselectpicklist',
        'pick_list_values': [{'display_value': v, 'actual_value': v} for v in SERVICE_DISPLAY]},
    (GAQL, 'Quote Lines Text'): {
        'field_label': 'Quote Lines Text', 'data_type': 'textarea', 'textarea': {'type': 'small'},
        'tooltip': DCMN_TOOLTIP},
    (GAQL, 'Lines Pending'): {'field_label': 'Lines Pending', 'data_type': 'integer', 'length': 9},
    (GAQL, 'WhatsApp Opt In'): {'field_label': 'WhatsApp Opt In', 'data_type': 'boolean'},
    ('Apostille_Services', 'Quote Request'): {
        'field_label': 'Quote Request', 'data_type': 'lookup',
        'lookup': {'display_label': 'Apostille from this quote', 'module': {'api_name': 'Get_A_Quote_Leads'}}},
    ('Apostille_Services', 'Created From'): {'field_label': 'Created From', 'data_type': 'website'},
    ('Apostille_Services', 'Translation Required'): {
        'field_label': 'Translation Required', 'data_type': 'picklist',
        'pick_list_values': [{'display_value': 'Yes -Translate', 'actual_value': 'Yes'},
                             {'display_value': 'No - Translation', 'actual_value': 'No'}]},
    ('Apostille_Services', 'Translation Source Language'): {
        'field_label': 'Translation Source Language', 'data_type': 'text', 'length': 60},
}
GAQL_BLOCKING = ('External_Order_Key', 'Name', 'GET_A_QUOTE_LEADS')


def zoho_down(call):
    raise requests.ConnectionError('Zoho unreachable')


class GatingTests(SchemaTestCase):
    def test_refresh_and_validate_skipped_when_engine_off_and_intake_off(self):
        with patch('crm.schema.validate_registry') as validate, patch('crm.schema.refresh') as refresh:
            result = refresh_zoho_schema.apply()

        self.assertEqual(result.result, 'skipped')
        refresh.assert_not_called()
        validate.assert_not_called()
        self.assertNoHttp()
        self.assertFalse(ZohoSchemaSnapshot.objects.exists())

    def test_refresh_and_validate_run_while_a_feature_is_on(self):
        for flag in ({'CRM_ENGINE_MODE': 'dry_run'}, {'QUOTE_V3_ENABLED': True}):
            with self.subTest(**flag), override_settings(**flag), \
                    patch('crm.schema.validate_registry', return_value=[]) as validate:
                self.http.calls.clear()
                result = refresh_zoho_schema.apply()

                self.assertTrue(result.successful(), result.traceback)
                validate.assert_called_once_with()
                self.assertEqual([(c.method, c.url, c.params) for c in self.http.calls],
                                 [('GET', FIELDS_URL, {'module': m}) for m in schema.REFRESH_MODULES])
        self.assertEqual(ZohoSchemaSnapshot.objects.count(), len(schema.REFRESH_MODULES))   # unchanged 2nd time

    @override_settings(CRM_ENGINE_MODE='dry_run')
    def test_registry_problems_alert_schema_drift(self):
        problems = ['Apostille_Services.Status: stage value not active']
        with patch('crm.schema.validate_registry', return_value=problems):
            result = refresh_zoho_schema.apply()

        self.assertIn('problems=1', result.result)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('schema drift', mail.outbox[0].subject)
        self.assertIn(problems[0], mail.outbox[0].body)

    def test_validate_registry_delegates_to_the_registry_with_cached_metadata(self):
        schema.refresh(GAQL)
        registry = types.ModuleType('crm.registry')
        registry.validate = lambda metadata: [f'seen {sorted(metadata)}']

        with patch.dict(sys.modules, {'crm.registry': registry}):
            self.assertEqual(schema.validate_registry(), [f"seen ['{GAQL}']"])


class ApplyTests(SchemaTestCase):
    def test_apply_posts_one_field_per_call_with_spec_payload(self):
        code, out = self.apply()

        self.assertEqual(code, 0, out)
        posts = self.field_posts()
        self.assertEqual(len(posts), 50)
        self.assertTrue(all(len(c.json['fields']) == 1 and set(c.json) == {'fields'} for c in posts))
        sent = {(c.params['module'], c.json['fields'][0]['field_label']): c.json['fields'][0] for c in posts}
        self.assertEqual(len(sent), 50)
        for key, example in Z3_EXAMPLES.items():
            with self.subTest(field=key):
                self.assertEqual(sent[key], example)
        self.assertEqual(self.sleep.call_args_list, [call(1.0)] * 49)   # 1 s apart
        self.assertIn('CREATED Lead_Language (id ', out)
        self.assertEqual([i for i in schema.plan() if i.action == schema.ADD], [])

    def test_rerun_after_apply_is_noop(self):
        self.assertEqual(self.apply()[0], 0)
        self.http.calls.clear()

        code, out = self.apply()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.field_posts(), [])
        self.assertEqual(self.zoho_writes(), [])
        self.assertIn('OK Lead_Language', out)
        self.assertIn('Nothing to create.', out)

    def test_aborts_when_derived_api_name_differs(self):
        self.zoho.derived['Lead Language'] = 'Lead_Language1'

        code, out = self.apply()

        self.assertEqual(code, 3)
        self.assertEqual(len(self.field_posts()), 1)
        self.assertIn('Zoho derived api_name Lead_Language1 (spec Lead_Language, A1)', out)
        log = self.run_logs()[-1]
        self.assertEqual((log['result'], log['exit_code']), ('aborted', 3))
        self.assertEqual(log['actions'][0]['reread']['api_name'], 'Lead_Language1')

    def test_stops_at_a_refused_type_and_names_the_fallback(self):
        self.zoho.refused['Services Requested'] = 'INVALID_DATA'

        code, out = self.apply('--module', GAQL)

        self.assertEqual(code, 3)
        self.assertEqual(self.field_posts()[-1].json['fields'][0]['field_label'], 'Services Requested')
        self.assertEqual(len(self.field_posts()), 7)
        self.assertIn('Services_Requested_Text', out)
        self.assertIsNone(self.zoho.field(GAQL, 'Services_Created'))

    def test_only_creates_the_named_fields(self):
        code, out = self.apply('--module', GAQL, '--only', 'Lead_Language')

        self.assertEqual(code, 0, out)
        self.assertEqual([c.json['fields'][0]['field_label'] for c in self.field_posts()], ['Lead Language'])
        self.sleep.assert_not_called()

    def test_never_sends_put_to_existing_field(self):
        before = json.dumps(self.zoho.field(GAQL, 'GET_A_QUOTE_LEADS'), sort_keys=True)

        code, out = self.apply()

        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in self.http.calls if c.method in ('PUT', 'PATCH', 'DELETE')], [])
        self.assertEqual({c.url for c in self.zoho_writes()}, {FIELDS_URL})   # never …/settings/fields/{id}
        labels = [c.json['fields'][0]['field_label'] for c in self.field_posts()]
        self.assertNotIn('GET A QUOTE LEADS', labels)
        self.assertNotIn('Web Quote Request', json.dumps([c.json for c in self.field_posts()]))
        self.assertEqual(json.dumps(self.zoho.field(GAQL, 'GET_A_QUOTE_LEADS'), sort_keys=True), before)
        existing = [(m, s.label) for m, specs in schema_spec.SPEC.items() for s in specs if s.existing]
        self.assertTrue(existing)
        self.assertFalse(set(existing) & {(c.params['module'], label) for c, label in zip(self.field_posts(), labels)})


class VerifyTests(SchemaTestCase):
    def test_manual_items_fail_until_seen_in_metadata(self):
        code, out = self.run_command('--verify')
        self.assertEqual(code, 1)                                  # the ADD items are not there yet
        self.assertIn('ADD Quote_Reference', out)

        self.zoho.create_api_fields()                              # ZOHO-SETUP Z3
        code, out = self.run_command('--verify')
        self.assertEqual(code, 1)
        self.assertNotIn('ADD ', out.replace('ADD 0', ''))
        self.assertIn('MANUAL GET_A_QUOTE_LEADS option Web Quote Request (ZOHO-SETUP Z4.1)', out)
        self.assertIn("OK Quote_Request related list title 'Apostille from this quote'", out)   # set at create

        self.zoho.add_option(GAQL, 'GET_A_QUOTE_LEADS', 'Web Quote Request')   # Z4.1
        code, out = self.run_command('--verify')
        self.assertEqual(code, 0, out)
        self.assertIn('OK GET_A_QUOTE_LEADS option Web Quote Request', out)
        self.assertIn('MANUAL Quote_Lines subform: create in the UI (ZOHO-SETUP Z11, not required yet)', out)

        with override_settings(QUOTE_V3_SUBFORM='auto'):
            code, out = self.run_command('--verify')
            self.assertEqual(code, 1)
            self.zoho.add_subform(GAQL, schema_spec.QUOTE_LINES)  # Z11
            code, out = self.run_command('--verify')
            self.assertEqual(code, 0, out)
        self.assertIn('OK Quote_Lines.Service_Key text 24', out)
        self.assertIn('OK Quote_Lines.Line_Status picklist (9 values)', out)

        self.assertEqual(self.zoho_writes(), [])
        self.assertEqual(self.run_logs(), [])

    def test_renamed_related_list_title_fails_verify_until_fixed_by_hand(self):
        self.zoho.create_api_fields()
        self.zoho.add_option(GAQL, 'GET_A_QUOTE_LEADS', 'Web Quote Request')
        self.zoho.field('Apostille_Services', 'Quote_Request')['lookup']['display_label'] = 'Related List Label 1'

        code, out = self.run_command('--verify')
        self.assertEqual(code, 1)
        self.assertIn("MANUAL Quote_Request related list title 'Apostille from this quote' (ZOHO-SETUP Z4.2)", out)

        self.zoho.set_related_list_titles()                        # Z4.2
        code, out = self.run_command('--verify')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.zoho_writes(), [])

    def test_registry_problems_fail_verify(self):
        self.zoho.create_api_fields()
        self.zoho.add_option(GAQL, 'GET_A_QUOTE_LEADS', 'Web Quote Request')
        self.zoho.set_related_list_titles()

        with patch('crm.schema.validate_registry', return_value=['Deals.Stage: value missing']) as validate:
            code, out = self.run_command('--verify')

        self.assertEqual(code, 1)
        self.assertIn('PROBLEM registry: Deals.Stage: value missing', out)
        (metadata,), _ = validate.call_args
        self.assertEqual(set(metadata), set(schema_spec.SPEC))
        self.assertIn('Quote_Reference', metadata[GAQL])


class LogTests(SchemaTestCase):
    def test_log_has_no_token(self):
        code, out = self.apply('--module', GAQL, '--only', 'Lead_Language')

        self.assertEqual(code, 0, out)
        authorizations = [(c.headers or {}).get('Authorization', '') for c in self.http.calls]
        self.assertIn(f'Zoho-oauthtoken {ZOHO_TEST_TOKEN}', authorizations)
        self.assertNotIn(f'Zoho-oauthtoken {REFRESHED_TOKEN}', authorizations)   # a scope 401 costs no refresh
        self.assertFalse([c for c in self.http.calls if c.url.startswith('https://accounts.zoho.com/')])
        (path,) = list(self.log_dir.glob('zoho_schema_*.json'))
        text = path.read_text(encoding='utf-8')
        for secret in ('Zoho-oauthtoken', 'Authorization', ZOHO_TEST_TOKEN, REFRESHED_TOKEN):
            self.assertNotIn(secret, text)
        log = json.loads(text)
        self.assertEqual((log['result'], log['exit_code'], log['org']), ('ok', 0, 'org882204370'))
        self.assertEqual([c['check'] for c in log['checks']], ['apply_allowed', 'confirm_org', 'gaql_layout', 'org'])
        (action,) = log['actions']
        self.assertEqual((action['api_name'], action['outcome']), ('Lead_Language', 'created'))
        self.assertEqual(action['reread']['id'], action['field_id'])
        self.assertEqual(action['response']['fields'][0]['code'], 'SUCCESS')


class GuardTests(SchemaTestCase):
    def setUp(self):
        super().setUp()
        for module in (GAQL, 'Apostille_Services'):
            schema.refresh(module)
        self.http.calls.clear()

    def test_unknown_field_dropped(self):
        result = schema.guard(GAQL, {'Name': 'Q-482913 · ZZ TEST', 'Quote_Reference': 'Q-482913'}, blocking=['Name'])

        self.assertEqual(result.payload, {'Name': 'Q-482913 · ZZ TEST'})   # Quote_Reference is not in Zoho yet
        self.assertEqual(result.dropped, [('Quote_Reference', 'Q-482913')])
        self.assertEqual(result.blocked, [])
        self.assertNoHttp()

    def test_off_list_picklist_value_dropped(self):
        payload = {'Service_Type': 'Same Day Service', 'Service_Area': 'Washington DC', 'Status': None}

        result = schema.guard('Apostille_Services', payload, blocking=[])

        self.assertEqual(result.payload, {'Service_Type': 'Same Day Service', 'Status': None})
        self.assertEqual(result.dropped, [('Service_Area', 'Washington DC')])

        self.zoho.create_api_fields()
        schema.refresh(GAQL)
        result = schema.guard(GAQL, {'Services_Requested': ['Apostille', 'Traducción jurada'],
                                     'Services_Created': ['Notary'], 'Primary_Service': 'Apostille'}, blocking=[])
        self.assertEqual(result.payload, {'Services_Requested': ['Apostille'], 'Primary_Service': 'Apostille'})
        self.assertEqual(result.dropped, [('Services_Requested', 'Traducción jurada'), ('Services_Created', 'Notary')])

    def test_refused_multiselect_written_to_its_text_fallback(self):
        """A2 false: Zoho has `Services_Requested_Text` (text 255) instead of the multi-select."""
        spec = schema_spec.find(GAQL, 'Services_Requested')
        self.zoho.add(GAQL, spec.fallback)
        schema.refresh(GAQL)

        result = schema.guard(GAQL, {'Services_Requested': ['Apostille', 'Translation']}, blocking=[])

        self.assertEqual(result.payload, {'Services_Requested_Text': 'Apostille, Translation'})
        self.assertEqual(result.dropped, [])

    def test_text_cut_to_length_with_ellipsis(self):
        long_text = 'x' * 300
        payload = {
            'Document_Type': long_text,                 # text 255
            'Client_Comments': 'y' * 2500,              # textarea 2000
            'Country_of_Use': 'Spain',
            'Name': 'n' * 121,                          # text 120, never cut
            'External_Order_Key': 'k' * 101,            # text 100, never cut
            'Client_Email': f'{"e" * 95}@example.com',  # email 100: a cut address is invalid
        }

        result = schema.guard(GAQL, payload, blocking=GAQL_BLOCKING)

        self.assertEqual(result.payload['Document_Type'], 'x' * 254 + '…')
        self.assertEqual(len(result.payload['Client_Comments']), 2000)
        self.assertTrue(result.payload['Client_Comments'].endswith('…'))
        self.assertEqual(result.payload['Country_of_Use'], 'Spain')
        self.assertNotIn('Name', result.payload)
        self.assertNotIn('External_Order_Key', result.payload)
        self.assertNotIn('Client_Email', result.payload)
        self.assertEqual(result.blocked, ['External_Order_Key', 'Name'])

    def test_blocking_field_dropped_marks_blocked(self):
        payload = {'Name': 'Q-482913 · ZZ TEST', 'External_Order_Key': 'dcmn:qr:57',
                   'GET_A_QUOTE_LEADS': 'Web Quote Request', 'Client_Comments': 'Receipt'}

        result = schema.guard(GAQL, payload, blocking=GAQL_BLOCKING)

        self.assertEqual(result.blocked, ['GET_A_QUOTE_LEADS'])   # never a fallback to a copying value
        self.assertEqual(result.dropped, [('GET_A_QUOTE_LEADS', 'Web Quote Request')])
        self.assertNotIn('GET_A_QUOTE_LEADS', result.payload)

        self.zoho.add_option(GAQL, 'GET_A_QUOTE_LEADS', 'Web Quote Request')   # ZOHO-SETUP Z4.1
        schema.refresh(GAQL)
        result = schema.guard(GAQL, payload, blocking=GAQL_BLOCKING)
        self.assertEqual((result.blocked, result.dropped, result.payload), ([], [], payload))

    def test_type_mismatch_dropped(self):
        result = schema.guard(GAQL, {'Number_Of_Documents': '3', 'Locked__s': 'yes'}, blocking=[])
        self.assertEqual(result.payload, {})
        self.assertEqual(result.dropped, [('Number_Of_Documents', '3'), ('Locked__s', 'yes')])

        result = schema.guard(GAQL, {'Number_Of_Documents': 3, 'Locked__s': False}, blocking=[])
        self.assertEqual(result.payload, {'Number_Of_Documents': 3, 'Locked__s': False})
        self.assertEqual(schema.guard(GAQL, {'Number_Of_Documents': True}, blocking=[]).payload, {})


class CacheTests(SchemaTestCase):
    def test_snapshot_fallback_when_zoho_unreachable(self):
        self.assertTrue(schema.refresh(GAQL))
        cache.clear()
        self.warm_zoho_token()
        self.http.handler = zoho_down

        with self.assertRaises(ZohoTransientError):
            schema.refresh(GAQL)
        with override_settings(CRM_ENGINE_MODE='dry_run'):
            result = refresh_zoho_schema.apply()
        self.assertTrue(result.successful(), result.traceback)
        self.assertIn(f'failed={len(schema.REFRESH_MODULES)}', result.result)

        fields = schema.get_fields(GAQL)
        self.assertEqual(fields['External_Order_Key']['length'], 100)
        result = schema.guard(GAQL, {'Name': 'Q-1 · ZZ TEST', 'External_Order_Key': 'dcmn:qr:1'},
                              blocking=GAQL_BLOCKING)
        self.assertEqual(result.blocked, [])
        self.assertEqual(ZohoSchemaSnapshot.objects.filter(module=GAQL).count(), 1)

    def test_no_snapshot_blocks_blocking_fields(self):
        payload = {'Name': 'Q-1 · ZZ TEST', 'External_Order_Key': 'dcmn:qr:1',
                   'GET_A_QUOTE_LEADS': 'Web Quote Request', 'Client_Comments': 'Receipt'}

        self.assertIsNone(schema.get_fields(GAQL))
        result = schema.guard(GAQL, payload, blocking=GAQL_BLOCKING)

        self.assertEqual(result.payload, {})
        self.assertEqual(result.blocked, list(GAQL_BLOCKING))
        self.assertEqual({name for name, _ in result.dropped}, set(payload))
        self.assertNoHttp()

    def test_refresh_saves_snapshot_only_on_change(self):
        self.assertTrue(schema.refresh(GAQL))
        self.assertFalse(schema.refresh(GAQL))
        self.assertEqual(ZohoSchemaSnapshot.objects.filter(module=GAQL).count(), 1)
        self.assertIsNotNone(cache.get(f'crm:schema:{GAQL}'))

        self.zoho.add_option(GAQL, 'GET_A_QUOTE_LEADS', 'Web Quote Request')
        self.zoho.field(GAQL, 'Detected_Country')['type'] = 'unused'   # "Mark as unused" (ZOHO-SETUP rollback)
        self.assertTrue(schema.refresh(GAQL))

        self.assertEqual(ZohoSchemaSnapshot.objects.filter(module=GAQL).count(), 2)
        latest = ZohoSchemaSnapshot.objects.filter(module=GAQL).latest()
        cache.clear()
        fields = schema.get_fields(GAQL)                                   # from the snapshot now
        self.assertEqual(fields, schema.by_api_name(latest.fields))
        self.assertIn('Web Quote Request', fields['GET_A_QUOTE_LEADS']['pick_list_values'])
        self.assertNotIn('Detected_Country', fields)
        self.assertEqual(set(fields['Client_Comments']), {'api_name', 'data_type', 'length', 'read_only',
                                                          'pick_list_values'})


class SpecTests(SchemaTestCase):
    def test_every_spec_api_name_is_label_with_underscores(self):
        def walk(specs):
            for spec in specs:
                yield spec
                yield from walk(spec.columns)
                if spec.fallback:
                    yield spec.fallback

        specs = [spec for module_specs in schema_spec.SPEC.values() for spec in walk(module_specs)]
        self.assertGreater(len(specs), 60)
        for spec in specs:
            with self.subTest(api_name=spec.api_name):
                self.assertEqual(spec.label.replace(' ', '_'), spec.api_name)   # A1

    def test_spec_fits_zoho_limits(self):
        for module, specs in schema_spec.SPEC.items():
            names = [spec.api_name for spec in specs]
            self.assertEqual(len(names), len(set(names)), module)
            self.assertLessEqual(sum(spec.data_type == 'lookup' for spec in specs), 1, module)
            for spec in specs:
                with self.subTest(module=module, api_name=spec.api_name):
                    self.assertTrue(all(len(value) <= 120 for value in spec.values))
                    self.assertLessEqual(len(spec.tooltip or ''), 32)
                    self.assertLessEqual(spec.length or 0, 2000)
        self.assertEqual(tuple(schema_spec.SERVICE_VALUES.values()), tuple(SERVICE_DISPLAY))
        self.assertEqual(schema_spec.MANUAL_OPTIONS, {(GAQL, 'GET_A_QUOTE_LEADS'): ('Web Quote Request',)})
