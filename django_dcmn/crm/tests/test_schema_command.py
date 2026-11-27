"""`zoho_schema` plan and apply guards (WP2). The shared fake Zoho settings API lives here.

No test reaches Zoho: `FakeZoho` answers through the NoNetworkTestCase spy, starting from the committed
baseline `fixtures/zoho_fields/fields_<module>.json` (copies of understand/zoho-dumps, body.fields only).
"""

import copy
import itertools
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from crm import schema_spec
from crm.schema_spec import GAQL, FieldSpec
from crm.testing import FakeResponse, HttpCall, NetworkBlocked, NoNetworkTestCase

FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'zoho_fields'
ORG = 'org882204370'
REFRESHED_TOKEN = 'refreshed-token-4f1c9a7e'
FIELDS_URL = 'https://www.zohoapis.com/crm/v6/settings/fields'
DEFAULT_LENGTHS = {'text': 255, 'textarea': 2000, 'integer': 9, 'boolean': 5, 'picklist': 120,
                   'multiselectpicklist': 2000, 'website': 255, 'lookup': 120, 'subform': 255}


def baseline_fields(module: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / f'fields_{module}.json').read_text(encoding='utf-8'))


class FakeZoho:
    """In-memory Zoho: field metadata per module, the GAQL layouts, `GET /crm/v6/org` and the OAuth refresh.

    A created field gets the api_name derived from its label (spaces → `_`) unless `derived` says otherwise.
    """

    def __init__(self):
        self.fields = {module: baseline_fields(module) for module in schema_spec.SPEC}
        self.layout_ids = [schema_spec.GAQL_LAYOUT_ID]
        self.org: dict | None = None                # None: 401 OAUTH_SCOPE_MISMATCH, like the prod token
        self.derived: dict[str, str] = {}           # label → api_name Zoho derives instead (A1 false)
        self.refused: dict[str, str] = {}           # label → Zoho error code
        self._ids = itertools.count(9000000000000000001)

    def __call__(self, call: HttpCall) -> FakeResponse:
        if call.url.startswith('https://accounts.zoho.com/oauth/'):
            return FakeResponse(200, {'access_token': REFRESHED_TOKEN})
        path = call.url.removeprefix('https://www.zohoapis.com/crm/')
        module = (call.params or {}).get('module')
        if (call.method, path) == ('GET', 'v2/settings/layouts') and module == GAQL:
            layouts = [{'id': layout_id, 'name': 'Standard'} for layout_id in self.layout_ids]
            return FakeResponse(200, {'layouts': layouts})
        if (call.method, path) == ('GET', 'v6/org'):
            if self.org is None:
                return FakeResponse(401, {'code': 'OAUTH_SCOPE_MISMATCH', 'details': {},
                                          'message': 'invalid oauth scope to access this URL', 'status': 'error'})
            return FakeResponse(200, {'org': [self.org]})
        if (call.method, path) == ('GET', 'v6/settings/fields'):
            if module not in self.fields:
                return FakeResponse(400, {'code': 'INVALID_MODULE', 'details': {}, 'status': 'error'})
            return FakeResponse(200, {'fields': copy.deepcopy(self.fields[module])})
        if (call.method, path) == ('POST', 'v6/settings/fields'):
            return self._create(module, call.json)
        raise NetworkBlocked(f'FakeZoho has no route for {call.method} {call.url}')

    def _create(self, module: str, body: dict) -> FakeResponse:
        (item,) = body['fields']
        label = item['field_label']
        if label in self.refused:
            return FakeResponse(400, {'fields': [{'code': self.refused[label], 'details': {}, 'status': 'error'}]})
        field = self.make_field(self.derived.get(label, label.replace(' ', '_')), item)
        self.fields[module].append(field)
        return FakeResponse(201, {'fields': [{'code': 'SUCCESS', 'details': {'id': field['id']},
                                              'message': 'field created', 'status': 'success'}]})

    def make_field(self, api_name: str, item: dict) -> dict:
        """A field as Zoho's metadata shows it, from a create payload."""
        data_type = item['data_type']
        values = list(item.get('pick_list_values') or [])
        if data_type == 'picklist':
            values.insert(0, {'display_value': '-None-', 'actual_value': '-None-'})
        field_id = str(next(self._ids))
        field = {
            'id': field_id, 'api_name': api_name, 'field_label': item['field_label'],
            'display_label': item['field_label'], 'data_type': data_type,
            'length': item.get('length') or DEFAULT_LENGTHS.get(data_type), 'custom_field': True, 'type': 'used',
            'read_only': False, 'lookup': {}, 'associated_module': None, 'tooltip': item.get('tooltip'),
            'pick_list_values': [{**value, 'id': f'{field_id}{n}', 'sequence_number': n, 'type': 'used'}
                                 for n, value in enumerate(values, 1)],
        }
        if data_type == 'textarea':
            field['textarea'] = item.get('textarea') or {'type': 'small'}
        if data_type == 'lookup':
            target = item['lookup']['module']['api_name']
            field['lookup'] = {'display_label': 'Related List Label 1', 'api_name': 'Related_List_Label_1',
                               'id': field_id, 'module': {'api_name': target, 'id': '6686316000000000001'}}
        return field

    # --- the manual (UI) steps and shortcuts -------------------------------------------------------

    def field(self, module: str, api_name: str) -> dict | None:
        return next((f for f in self.fields[module] if f['api_name'] == api_name), None)

    def add(self, module: str, spec: FieldSpec, **overrides) -> dict:
        field = {**self.make_field(spec.api_name, spec.create_payload()), **overrides}
        self.fields[module].append(field)
        return field

    def create_api_fields(self) -> None:
        """The state after ZOHO-SETUP Z3: every API field exists (as if `--apply` had run)."""
        for module, specs in schema_spec.SPEC.items():
            for spec in specs:
                if not (spec.manual or spec.existing) and self.field(module, spec.api_name) is None:
                    self.add(module, spec)

    def add_option(self, module: str, api_name: str, value: str) -> None:
        options = self.field(module, api_name)['pick_list_values']
        options.append({'display_value': value, 'actual_value': value, 'id': f'opt{len(options)}',
                        'sequence_number': len(options) + 1, 'type': 'used'})

    def set_related_list_titles(self) -> None:
        """ZOHO-SETUP Z4.2."""
        for module, specs in schema_spec.SPEC.items():
            for spec in specs:
                if spec.related_list_title:
                    self.field(module, spec.api_name)['lookup']['display_label'] = spec.related_list_title

    def add_subform(self, module: str, spec: FieldSpec) -> None:
        """ZOHO-SETUP Z11: the subform and its columns (a separate metadata module)."""
        self.add(module, spec, associated_module={'module': spec.api_name, 'id': '6686316000099000001'})
        self.fields[spec.api_name] = [self.make_field(column.api_name, column.create_payload())
                                      for column in spec.columns]


@override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=False, ZOHO_ORG_ID=ORG, QUOTE_V3_SUBFORM='off',
                   CRM_ENGINE_MODE='off', QUOTE_V3_ENABLED=False)
class SchemaTestCase(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.warm_zoho_token()
        self.zoho = FakeZoho()
        self.http.handler = self.zoho
        self.log_dir = Path(tempfile.mkdtemp(prefix='zoho_schema_test_'))
        self.addCleanup(shutil.rmtree, self.log_dir, ignore_errors=True)
        log_settings = override_settings(ZOHO_SCHEMA_LOG_DIR=str(self.log_dir))
        log_settings.enable()
        self.addCleanup(log_settings.disable)
        sleep = patch('crm.management.commands.zoho_schema.time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_command(self, *args: str) -> tuple[int, str]:
        """(exit code, stdout + error message) of `zoho_schema`."""
        out = StringIO()
        try:
            call_command('zoho_schema', *args, stdout=out, stderr=StringIO())
        except CommandError as exc:
            return exc.returncode, f'{out.getvalue()}\n{exc}'
        return 0, out.getvalue()

    def apply(self, *args: str) -> tuple[int, str]:
        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True):
            return self.run_command('--apply', '--confirm-org', ORG, *args)

    def zoho_writes(self) -> list[HttpCall]:
        """Writes to the CRM API (the OAuth refresh to accounts.zoho.com is not one)."""
        return [c for c in self.http.calls if c.method != 'GET' and c.url.startswith('https://www.zohoapis.com/')]

    def field_posts(self) -> list[HttpCall]:
        return [c for c in self.zoho_writes() if c.method == 'POST' and c.url == FIELDS_URL]

    def run_logs(self) -> list[dict]:
        paths = sorted(self.log_dir.glob('zoho_schema_*.json'))
        return [json.loads(path.read_text(encoding='utf-8')) for path in paths]


# DESIGN §6.1–6.3 = ZOHO-SETUP Z3.2 (50 fields), in apply order.
EXPECTED_ADD = [
    *[(GAQL, name) for name in (
        'Lead_Language', 'Quote_Reference', 'Quote_Status', 'Service_Progress', 'Lines_Pending', 'Primary_Service',
        'Services_Requested', 'Services_Created', 'Quote_Lines_Text', 'Urgency', 'Contact_Preference',
        'WhatsApp_Opt_In', 'SMS_Opt_In', 'Consent_Version', 'Quote_Source_Page')],
    *[(module, name) for module in ('Apostille_Services', 'Embassy_Legalization') for name in (
        'Quote_Request', 'Quote_Reference', 'Created_From', 'Translation_Required',
        'Translation_Source_Language', 'Translation_Target_Language')],
    *[('Translation_Services', name) for name in ('Quote_Request', 'Quote_Reference', 'Created_From')],
    *[('FINGERPRINT_SERVICES', name) for name in (
        'Quote_Request', 'Quote_Reference', 'Created_From',
        'Translation_Source_Language', 'Translation_Target_Language')],
    *[(module, name) for module in ('Notary_Services', 'I_9_Verification')
      for name in ('Quote_Request', 'Quote_Reference', 'Created_From')],
    *[('Deals', name) for name in (
        'Quote_Request', 'Quote_Reference', 'Created_From',
        'Translation_Source_Language', 'Translation_Target_Language')],
    *[('Triple_Seal_Apostilles', name) for name in (
        'Quote_Reference', 'Created_From', 'Translation_Source_Language', 'Translation_Target_Language')],
]
TITLED_MODULES = ('Apostille_Services', 'Embassy_Legalization', 'Translation_Services', 'FINGERPRINT_SERVICES',
                  'Notary_Services', 'I_9_Verification', 'Deals')
EXPECTED_MANUAL = {
    (GAQL, 'GET_A_QUOTE_LEADS', 'option', 'option Web Quote Request'),
    (GAQL, 'Quote_Lines', 'field', 'subform: create in the UI'),
    *[(module, 'Quote_Request', 'related_list', f"related list title '{title}'")
      for module, title in zip(TITLED_MODULES, (
          'Apostille from this quote', 'Embassy from this quote', 'Translation from this quote',
          'Fingerprinting / FBI from this quote', 'Notary from this quote', 'I-9 from this quote',
          'FBI apostille from this quote'))],
}
EXPECTED_EXISTING_OK = {
    (GAQL, 'GET_A_QUOTE_LEADS'), ('FINGERPRINT_SERVICES', 'Translation_Needed'), ('Deals', 'Translation_Required'),
    ('Deals', 'Translated_Language'), ('Triple_Seal_Apostilles', 'Translation_Required'),
}


class PlanTests(SchemaTestCase):
    def test_plan_against_baseline_lists_exactly_the_design_fields(self):
        target = self.log_dir / 'plan' / 'zoho_plan_before.json'

        code, out = self.run_command('--json', str(target))

        self.assertEqual(code, 0, out)
        document = json.loads(target.read_text(encoding='utf-8'))
        plan = document['plan']
        self.assertEqual([(i['module'], i['api_name']) for i in plan if i['action'] == 'ADD'], EXPECTED_ADD)
        self.assertEqual(len(EXPECTED_ADD), 50)
        manual = {(i['module'], i['api_name'], i['kind'], i['detail']) for i in plan if i['action'] == 'MANUAL'}
        self.assertEqual(manual, EXPECTED_MANUAL)
        self.assertEqual({(i['module'], i['api_name']) for i in plan if i['action'] == 'OK'}, EXPECTED_EXISTING_OK)
        self.assertEqual([i for i in plan if i['action'] in ('MISMATCH', 'BLOCKED')], [])
        self.assertTrue(all('payload' in i for i in plan if i['action'] == 'ADD'))
        self.assertEqual(set(document['metadata']), set(schema_spec.SPEC))   # the 9 modules (ZOHO-SETUP Z1)
        self.assertIn('MANUAL GET_A_QUOTE_LEADS option Web Quote Request (ZOHO-SETUP Z4.1)', out)
        self.assertIn('ADD Quote_Reference text 20', out)
        self.assertIn('ADD 50 · OK 5 · MANUAL 9 · MISMATCH 0 · BLOCKED 0', out)
        self.assertEqual((target.parent / '.gitignore').read_text(encoding='utf-8').splitlines()[-1], '*')

    def test_plan_makes_only_get_requests(self):
        code, out = self.run_command()

        self.assertEqual(code, 0, out)
        self.assertEqual({c.method for c in self.http.calls}, {'GET'})
        self.assertEqual([(c.url, c.params) for c in self.http.calls],
                         [(FIELDS_URL, {'module': module}) for module in schema_spec.SPEC])
        self.assertEqual(self.run_logs(), [])                     # a plan writes no run log
        self.assertEqual(list(self.log_dir.iterdir()), [])

    def test_mismatch_exits_1_and_is_never_fixed(self):
        short = self.zoho.add(GAQL, schema_spec.QUOTE_REFERENCE, length=10)

        code, out = self.run_command('--module', GAQL)
        self.assertEqual(code, 1)
        self.assertIn('MISMATCH Quote_Reference length 10 (spec 20)', out)

        code, out = self.apply()
        self.assertEqual(code, 1)
        self.assertEqual(self.zoho_writes(), [])
        self.assertEqual(self.zoho.field(GAQL, 'Quote_Reference'), short)
        self.assertEqual(self.run_logs()[-1]['result'], 'plan_problem')

    def test_fourth_lookup_on_triple_seal_is_blocked(self):
        self.assertIsNone(schema_spec.find('Triple_Seal_Apostilles', 'Quote_Request'))   # not in the spec [F3]
        lookup = schema_spec.find('Apostille_Services', 'Quote_Request')
        triple_seal = [lookup, *schema_spec.SPEC['Triple_Seal_Apostilles']]

        with patch.dict(schema_spec.SPEC, {'Triple_Seal_Apostilles': triple_seal}):
            code, out = self.run_command('--module', 'Triple_Seal_Apostilles')

        self.assertEqual(code, 1)
        self.assertIn('BLOCKED Quote_Request would be custom lookup #4', out)

    def test_name_clash_is_blocked(self):
        clash = self.zoho.make_field('Quote_Ref', {'field_label': 'Quote Reference', 'data_type': 'text'})
        self.zoho.fields['Notary_Services'].append(clash)

        code, out = self.run_command('--module', 'Notary_Services')

        self.assertEqual(code, 1)
        self.assertIn('BLOCKED Quote_Reference name clash with existing field Quote_Ref', out)

    def test_unknown_module_is_refused_without_http(self):
        code, out = self.run_command('--module', 'Leads')

        self.assertEqual(code, 2)
        self.assertNoHttp()


class ApplyGuardTests(SchemaTestCase):
    def test_refuses_without_apply_flag(self):
        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True):
            code, out = self.run_command('--confirm-org', ORG, '--only', 'Lead_Language')

        self.assertEqual(code, 0, out)
        self.assertIn('Plan only', out)
        self.assertEqual({c.method for c in self.http.calls}, {'GET'})
        self.assertEqual(self.zoho_writes(), [])
        self.assertIsNone(self.zoho.field(GAQL, 'Lead_Language'))

    def test_refuses_without_env_allowed(self):
        code, out = self.run_command('--apply', '--confirm-org', ORG)

        self.assertEqual(code, 2)
        self.assertIn('ZOHO_SCHEMA_APPLY_ALLOWED=1', out)
        self.assertNoHttp()
        self.assertEqual(self.run_logs()[-1]['result'], 'refused')

    def test_refuses_on_org_mismatch(self):
        code, out = self.apply()   # wrong org is in the next two runs; this one proves the guard can pass
        self.assertEqual(code, 0, out)
        self.http.calls.clear()

        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True):
            code, _ = self.run_command('--apply', '--confirm-org', 'org000000001')
        self.assertEqual(code, 2)
        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True, ZOHO_ORG_ID='org000000001'):
            code, _ = self.run_command('--apply', '--confirm-org', ORG)
        self.assertEqual(code, 2)
        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True):
            code, _ = self.run_command('--apply')
        self.assertEqual(code, 2)
        self.assertNoHttp()

    def test_refuses_when_org_endpoint_names_another_org(self):
        self.zoho.org = {'domain_name': 'org000000001', 'zgid': '000000001'}

        code, out = self.apply()

        self.assertEqual(code, 2)
        self.assertIn('GET /crm/v6/org is not org882204370', out)
        self.assertEqual(self.zoho_writes(), [])

    def test_org_endpoint_is_checked_when_the_token_allows_it(self):
        self.zoho.org = {'domain_name': ORG, 'zgid': '882204370'}

        code, out = self.apply('--module', GAQL, '--only', 'Lead_Language')

        self.assertEqual(code, 0, out)
        self.assertIn({'check': 'org', 'ok': True}, self.run_logs()[-1]['checks'])

    def test_refuses_when_layout_fingerprint_missing(self):
        self.zoho.layout_ids = ['6686316000000000042']

        code, out = self.apply()

        self.assertEqual(code, 2)
        self.assertIn('6686316000002178172', out)
        self.assertEqual([(c.method, c.url) for c in self.http.calls],
                         [('GET', 'https://www.zohoapis.com/crm/v2/settings/layouts')])

    def test_refuses_when_plan_has_mismatch(self):
        self.zoho.add(GAQL, FieldSpec('Lead_Language', 'Lead Language', 'text', length=60))

        code, out = self.apply('--only', 'Quote_Reference')

        self.assertEqual(code, 1)
        self.assertIn('MISMATCH Lead_Language type text (spec picklist)', out)
        self.assertEqual(self.field_posts(), [])
        self.assertEqual(self.zoho_writes(), [])

    def test_refuses_only_names_outside_the_spec(self):
        with override_settings(ZOHO_SCHEMA_APPLY_ALLOWED=True):
            code, out = self.run_command('--apply', '--confirm-org', ORG, '--only', 'GET_A_QUOTE_LEADS')

        self.assertEqual(code, 2)
        self.assertNoHttp()
