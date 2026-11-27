"""`manage.py e2e_quote` and `e2e_cleanup` (DESIGN §14.3 E12): guards, dry runs and the per-module rules."""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import override_settings

from crm.models import EngineRequest, ServiceLink
from crm.registry import COPY_RULE_IDS
from crm.testing import FakeResponse
from crm.tests.support import EngineTestCase, make_link, saved_quote
from crm.zoho import KEY_FIELD
from orders.models import Track
from quotes.models import GAQL_MODULE, QuoteRejection, QuoteSubmission
from quotes.pipeline import ATTRIBUTION_MODULE

ORG = 'org882204370'
RUN = 'r1'
APOSTILLE = 'Apostille_Services'
COPY_RULE = sorted(COPY_RULE_IDS)[0]


def copy_event(module: str, record_id: str) -> dict:
    return {'action': 'create_record', 'source': 'workflow', 'audited_time': '2026-11-26T15:19:10+06:00',
            'automation_details': {'create_record': {'module': {'name': module}, 'id': record_id},
                                   'rule': {'id': COPY_RULE}}}


def added_by(rule_id: str) -> dict:
    return {'action': 'added', 'source': 'workflow', 'audited_time': '2026-11-26T15:19:10+06:00',
            'automation_details': {'rule': {'id': rule_id}, 'type': 'workflow_rule'}}


@override_settings(E2E_EMAIL_BASE='vlad+e2e@example.com')
class E2ETests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.deleted: list[str] = []
        crm = self.crm

        def handler(call):
            if call.method == 'DELETE':
                path = call.url.removeprefix('https://www.zohoapis.com/crm/v2/')
                module, record_id = path.split('/')
                self.deleted.append(path)
                crm.records[module].pop(record_id, None)
                return FakeResponse(200, {'data': [{'code': 'SUCCESS', 'status': 'success',
                                                    'details': {'id': record_id}}]})
            return crm(call)

        self.http.handler = handler

    def run_command(self, name: str, **options) -> str:
        out = StringIO()
        call_command(name, run=RUN, manifest_dir=str(self.dir), stdout=out, stderr=StringIO(), **options)
        return out.getvalue()

    def manifest_file(self) -> Path:
        return self.dir / f'e2e-{RUN}.json'

    def test_execute_refused_without_env(self):
        for name in ('e2e_quote', 'e2e_cleanup'):
            with self.subTest(name), self.assertRaisesMessage(CommandError, 'E2E_ALLOWED=1'):
                self.run_command(name, execute=True, confirm_org=ORG)
            with self.subTest(name, confirm='missing'), self.assertRaisesMessage(CommandError, '--confirm-org'):
                self.run_command(name, dry_run=True)
            with self.subTest(name, confirm='other org'), override_settings(E2E_ALLOWED=True), \
                    self.assertRaisesMessage(CommandError, '--confirm-org'):
                self.run_command(name, execute=True, confirm_org='org000000000')
        with self.assertRaisesMessage(CommandError, '--run'):
            call_command('e2e_quote', run='../etc', dry_run=True, confirm_org=ORG, manifest_dir=str(self.dir))
        self.assertNoHttp()
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_dry_run_writes_nothing(self):
        out = self.run_command('e2e_quote', dry_run=True, confirm_org=ORG)

        self.assertIn('ZZ TEST r1 Maria', out)
        self.assertIn('vlad+e2e-r1-e1@example.com', out)
        self.assertIn('+1 202 555 01', out)
        self.assertIn('E14 · Auto-intake', out)
        self.assertNotIn('E13 ·', out)   # S8 only, on request
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertEqual((QuoteSubmission.objects.count(), QuoteRejection.objects.count(),
                          EngineRequest.objects.count()), (0, 0, 0))
        self.assertNoHttp()

        quote = saved_quote(full_name=f'ZZ TEST {RUN} Maria')
        self.gaql_record(quote, Client_Name=quote.full_name)
        target = self.crm.add(APOSTILLE, Name=f'ZZ TEST · Apostille · {quote.full_name}')
        manifest = {'run': RUN, 'quotes': [{'pk': quote.pk, 'gaql_id': quote.zoho_record_id}],
                    'records': [{'module': APOSTILLE, 'id': target['id'], 'service': 'apostille'}],
                    'copies': [], 'tids': []}
        self.manifest_file().write_text(json.dumps(manifest))
        before = self.manifest_file().read_bytes()

        out = self.run_command('e2e_cleanup', dry_run=True, confirm_org=ORG)

        self.assertIn('DELETE record', out)
        self.assertIn('2 of', out)   # attribution row is gone already
        self.assertEqual(self.http.writes, [])
        self.assertEqual(self.deleted, [])
        self.assertTrue(QuoteSubmission.objects.filter(pk=quote.pk).exists())
        self.assertEqual(self.manifest_file().read_bytes(), before)

    @override_settings(E2E_ALLOWED=True)
    def test_cleanup_filters_per_module(self):
        quote = saved_quote(full_name=f'ZZ TEST {RUN} Maria', zoho_contact_action='created')
        real = saved_quote('v5_success', zoho_record_id='6686316000044444444', zoho_contact_id='6686316000000900002',
                           zoho_contact_action='matched')
        self.gaql_record(quote, Client_Name=quote.full_name)
        self.gaql_record(real, Client_Name=real.full_name)
        self.crm.add('Contacts', id=quote.zoho_contact_id, Full_Name=quote.full_name)
        self.crm.add('Contacts', id=real.zoho_contact_id, Full_Name=f'ZZ TEST {RUN} Maria')   # matched: kept
        ours = self.crm.add(ATTRIBUTION_MODULE, **{KEY_FIELD: f'dcmn:attribution:qr:{quote.pk}'})
        theirs = self.crm.add(ATTRIBUTION_MODULE, **{KEY_FIELD: f'dcmn:attribution:qr:{quote.pk + 1000}'})
        target = self.crm.add(APOSTILLE, Name=f'ZZ TEST · Apostille · {quote.full_name}')
        not_test = self.crm.add('Embassy_Legalization', Name='Embassy · Maria Fernández')
        copy = self.crm.add(APOSTILLE, Name='Apostille Service', Tracking_ID='COPYTID001')
        not_copy = self.crm.add(APOSTILLE, Name='Apostille Service')
        self.crm.timelines[(GAQL_MODULE, quote.zoho_record_id)] = [copy_event(APOSTILLE, copy['id']),
                                                                    copy_event(APOSTILLE, not_copy['id'])]
        self.crm.timelines[(APOSTILLE, copy['id'])] = [added_by(COPY_RULE)]
        self.crm.timelines[(APOSTILLE, not_copy['id'])] = [added_by('6686316000099999999')]   # not a copy rule
        Track.objects.create(tid='OURTID0001', data={'origin': 'crm', 'record_id': target['id']})
        Track.objects.create(tid='COPYTID001', data={'record_id': copy['id']})
        Track.objects.create(tid='FOREIGN001', data={'record_id': '6686316000012345678'})
        link = make_link(quote, 'apostille', state='created', target_record_id=target['id'], tracking_id='OURTID0001')
        manifest = {
            'run': RUN,
            'quotes': [{'pk': quote.pk, 'gaql_id': quote.zoho_record_id, 'contact_id': quote.zoho_contact_id,
                        'contact_action': 'created', 'attribution_id': ours['id']},
                       {'pk': real.pk, 'gaql_id': real.zoho_record_id, 'contact_id': real.zoho_contact_id,
                        'contact_action': 'matched', 'attribution_id': theirs['id']}],
            'records': [{'module': APOSTILLE, 'id': target['id'], 'service': 'apostille', 'link_id': link.pk},
                        {'module': 'Embassy_Legalization', 'id': not_test['id'], 'service': 'embassy'}],
            'copies': [{'module': APOSTILLE, 'id': copy['id'], 'gaql_id': quote.zoho_record_id},
                       {'module': APOSTILLE, 'id': not_copy['id'], 'gaql_id': quote.zoho_record_id}],
            'tids': ['OURTID0001', 'FOREIGN001'],
        }
        self.manifest_file().write_text(json.dumps(manifest))

        out = self.run_command('e2e_cleanup', execute=True, confirm_org=ORG)

        self.assertEqual(sorted(self.deleted), sorted([
            f'{APOSTILLE}/{target["id"]}', f'{APOSTILLE}/{copy["id"]}', f'{ATTRIBUTION_MODULE}/{ours["id"]}',
            f'{GAQL_MODULE}/{quote.zoho_record_id}', f'Contacts/{quote.zoho_contact_id}']))
        for kept in (not_test['id'], not_copy['id'], theirs['id'], real.zoho_record_id, real.zoho_contact_id):
            self.assertFalse(any(path.endswith(f'/{kept}') for path in self.deleted), kept)
        self.assertTrue(all(c.params == {'wf_trigger': 'false'} for c in self.http.calls if c.method == 'DELETE'))
        self.assertEqual(sorted(Track.objects.values_list('tid', flat=True)), ['FOREIGN001'])
        self.assertFalse(QuoteSubmission.objects.filter(pk=quote.pk).exists())
        self.assertFalse(ServiceLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(QuoteSubmission.objects.filter(pk=real.pk).exists())   # not a ZZ TEST quote
        self.assertIn('deleted 7 item(s), 0 failed', out)
        self.assertEqual(len(json.loads(self.manifest_file().read_text())['cleanup']['deleted']), 7)

    @override_settings(E2E_ALLOWED=True, QUOTE_V3_ENABLED=True)
    def test_execute_collects_manifest_and_runs_checks(self):
        quote = saved_quote(full_name=f'ZZ TEST {RUN} Maria', zoho_attribution_id='6686316000077700009')
        self.gaql_record(quote, Client_Name=quote.full_name)
        target = self.crm.add(APOSTILLE, Name=f'ZZ TEST · Apostille · {quote.full_name}')
        link = make_link(quote, 'apostille', state='created', origin='created', target_record_id=target['id'],
                         tracking_id='OURTID0001')
        target[KEY_FIELD] = link.external_key

        out = self.run_command('e2e_quote', execute=True, confirm_org=ORG, scenario='e1,E3,e4,E10b')

        manifest = json.loads(self.manifest_file().read_text())
        self.assertEqual(manifest['quotes'][0]['attribution_key'], f'dcmn:attribution:qr:{quote.pk}')
        self.assertEqual(manifest['records'][0]['id'], target['id'])
        self.assertEqual(manifest['tids'], ['OURTID0001'])
        self.assertIn('PASS     E3 honeypot', out)
        self.assertIn('PASS     E3 time_trap', out)
        self.assertIn('PASS     E1 GAQL timeline has no create_record', out)
        self.assertIn(f'PASS     E5 apostille {APOSTILLE}/{target["id"]}: key finds this record only', out)
        self.assertIn('BY HAND  E10b', out)
        self.assertEqual(QuoteRejection.objects.count(), 2)
        self.assertEqual([c for c in self.http.writes if c.url.startswith('https://www.zohoapis.com/')], [])
