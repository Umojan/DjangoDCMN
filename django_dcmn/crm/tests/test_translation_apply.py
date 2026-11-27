"""WP7 write to Zoho (DESIGN §10.3): `crm.tasks.apply_translation_signals` against `FakeCrm`.

A signal waits for its synced `ZohoSyncJob`, fills the empty translation fields of the job's actual record
(one PUT, `trigger: []`, through the guard), posts one Note and runs the rule `translation_from_form`.
"""

from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import override_settings
from django.utils import timezone

from crm import engine, translation_capture as tc
from crm.facts import SOURCE_FIELD_MAPS
from crm.models import EngineRequest, ServiceLink, ServiceSignal
from crm.tasks import apply_translation_signals
from crm.tests.support import EngineTestCase
from crm.tests.test_translation_capture import FORMS, TICKED
from crm.writeback import note_title, write_back
from crm.zoho import KEY_FIELD
from orders.models import FbiServicePackage, ShippingOption, ZohoSyncJob
from orders.zoho_errors import ZohoPermanentError
from quotes.models import GAQL_MODULE
from quotes.render import ET

# The order type's own module, and where that module keeps the client's name.
MODULES = {'fbi': 'Deals', 'apostille': 'Apostille_Services', 'embassy': 'Embassy_Legalization',
           'marriage': 'Triple_Seal_Apostilles', 'fingerprinting': 'FINGERPRINT_SERVICES'}
NAME_FIELDS = {'Deals': 'Deal_Name', 'Apostille_Services': 'Client_Name', 'Embassy_Legalization': 'Client_Name',
               'Triple_Seal_Apostilles': 'Client_Name', 'FINGERPRINT_SERVICES': 'Name', GAQL_MODULE: 'Client_Name'}
EMAIL_FIELDS = {'Deals': 'Email_1', 'Apostille_Services': 'Client_Email', 'Embassy_Legalization': 'Email',
                'Triple_Seal_Apostilles': 'Client_Email', 'FINGERPRINT_SERVICES': 'Email'}
CLIENT_NAME = 'ZZ TEST Translation Client'
CLIENT_EMAIL = 'zz.translation@example.com'
YES_FIELDS = {'Translation_Required': 'Yes -Translate', 'Translation_Source_Language': 'Spanish',
              'Translation_Target_Language': 'English'}
NO_VALUES = ('No - Translation', 'NO', False)

# The LATER source rows (DESIGN §7.2) a module needs before the engine accepts it as a source; test-only here.
FIELD_MAPS = {module: {'client_name': NAME_FIELDS[module], 'email': EMAIL_FIELDS[module]} for module in EMAIL_FIELDS}
ONE_CLICK = override_settings(CRM_SOURCES_ALLOWED=[GAQL_MODULE, *FIELD_MAPS])


@override_settings(TRANSLATION_CAPTURE_ENABLED=True)
class TranslationTestCase(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.next_order = iter(range(943, 2000))

    def signal(self, order_type: str = 'apostille', **answers) -> ServiceSignal:
        return tc.capture({**TICKED, **answers}, order_type, next(self.next_order))

    def synced(self, signal: ServiceSignal, module: str | None = None, **fields) -> dict:
        """The legacy sync's record (keyed `dcmn:{type}:{id}`) and its ZohoSyncJob."""
        module = module or MODULES[signal.order_type]
        record = self.crm.add(module, **{KEY_FIELD: f'dcmn:{signal.order_type}:{signal.order_id}',
                                         NAME_FIELDS[module]: CLIENT_NAME,
                                         EMAIL_FIELDS.get(module, 'Client_Email'): CLIENT_EMAIL, **fields})
        ZohoSyncJob.objects.create(order_type=signal.order_type, order_id=signal.order_id,
                                   status=ZohoSyncJob.STATUS_SYNCED, zoho_module=module, zoho_record_id=record['id'])
        return record

    def run_task(self) -> str:
        return apply_translation_signals()

    def put_fields(self, module: str) -> list[dict]:
        return [{k: v for k, v in call.json['data'][0].items() if k != 'id'} for call in self.puts(module)]

    def notes(self, module: str, record: dict) -> list[dict]:
        return self.crm.notes[(module, record['id'])]

    def note_writes(self) -> list:
        return [c for c in self.zoho_writes() if c.url.endswith('/v2/Notes')]


class ApplyTests(TranslationTestCase):
    def test_waits_for_synced_job(self):
        signal = self.signal('apostille')
        self.assertEqual(self.run_task(), 'expired=0')
        self.assertNoHttp()

        job = ZohoSyncJob.objects.create(order_type='apostille', order_id=signal.order_id,
                                         status=ZohoSyncJob.STATUS_PENDING)
        self.run_task()
        job.status, job.zoho_module = ZohoSyncJob.STATUS_SYNCED, 'Apostille_Services'   # no record id yet
        job.save()
        self.run_task()
        self.assertNoHttp()
        signal.refresh_from_db()
        self.assertEqual(signal.state, 'waiting_source')

        job.delete()
        record = self.synced(signal)
        self.assertIn('ignored=1', self.run_task())   # engine refuses the module until its LATER row exists
        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.source_module, signal.source_record_id),
                         ('evaluated', 'Apostille_Services', record['id']))
        self.assertEqual(self.put_fields('Apostille_Services'), [YES_FIELDS])

    def test_writes_actual_job_module(self):
        signal = self.signal('embassy')
        record = self.synced(signal, 'Apostille_Services')   # the order merged into a phone lead elsewhere

        self.run_task()

        (put,) = self.puts('Apostille_Services')
        self.assertEqual(put.json['data'][0]['id'], record['id'])
        self.assertEqual(self.puts('Embassy_Legalization'), [])
        self.assertEqual(self.crm.records['Apostille_Services'][record['id']]['Translation_Required'], 'Yes -Translate')
        (note,) = self.notes('Apostille_Services', record)
        self.assertIn('on the Embassy form', note['Note_Content'])

    def test_deals_fields_and_translated_language_only_if_in_list(self):
        spanish = self.signal('fbi', translation_source_language='English', translation_target_language='español')
        german = self.signal('fbi', translation_source_language='English', translation_target_language='German')
        self.synced(spanish)
        self.synced(german)

        self.run_task()

        self.assertEqual(self.put_fields('Deals'), [
            {'Translation_Required': 'Yes -Translate', 'Translation_Source_Language': 'English',
             'Translation_Target_Language': 'Spanish', 'Translated_Language': 'Spanish'},
            {'Translation_Required': 'Yes -Translate', 'Translation_Source_Language': 'English',
             'Translation_Target_Language': 'German'},
        ])

    def test_apostille_embassy_new_fields(self):
        records = {order_type: self.synced(self.signal(order_type)) for order_type in ('apostille', 'embassy')}

        self.run_task()

        for order_type, record in records.items():
            module = MODULES[order_type]
            with self.subTest(module):
                self.assertEqual(self.put_fields(module), [YES_FIELDS])
                (note,) = self.notes(module, record)
                label = 'Apostille' if order_type == 'apostille' else 'Embassy'
                self.assertEqual(note['Note_Content'],
                                 f'Client asked for a certified translation (Spanish → English) on the {label} form. '
                                 'Create it in Translation Services (one-click button coming).')

    def test_triple_seal_boolean(self):
        self.synced(self.signal('marriage'))

        self.run_task()

        (fields,) = self.put_fields('Triple_Seal_Apostilles')
        self.assertIs(fields['Translation_Required'], True)
        self.assertEqual(fields, {**YES_FIELDS, 'Translation_Required': True})

    def test_fingerprint_yes(self):
        self.synced(self.signal('fingerprinting'))

        self.run_task()

        self.assertEqual(self.put_fields('FINGERPRINT_SERVICES'), [
            {'Translation_Needed': 'YES', 'Translation_Source_Language': 'Spanish',
             'Translation_Target_Language': 'English'}])

    def test_fill_empty_only(self):
        partly = self.synced(self.signal('apostille'), Translation_Required='-None-',
                             Translation_Source_Language='French', Translation_Target_Language='')
        manager_said_no = self.synced(self.signal('apostille'), Translation_Required='No - Translation')
        full = self.synced(self.signal('apostille'), **YES_FIELDS)

        self.run_task()

        self.assertEqual([call.json['data'][0]['id'] for call in self.puts('Apostille_Services')],
                         [partly['id'], manager_said_no['id']])
        self.assertEqual(self.put_fields('Apostille_Services'), [
            {'Translation_Required': 'Yes -Translate', 'Translation_Target_Language': 'English'},
            {'Translation_Source_Language': 'Spanish', 'Translation_Target_Language': 'English'},
        ])
        stored = self.crm.records['Apostille_Services']
        self.assertEqual(stored[partly['id']]['Translation_Source_Language'], 'French')
        self.assertEqual(stored[manager_said_no['id']]['Translation_Required'], 'No - Translation')
        for record in (partly, manager_said_no, full):   # no field to fill still gets the Note
            self.assertEqual(len(self.notes('Apostille_Services', record)), 1)

    def test_never_writes_no_value(self):
        for order_type in MODULES:
            self.synced(self.signal(order_type))
            self.synced(self.signal(order_type, translation_source_language='', translation_target_language=''))

        self.run_task()

        puts = [call for module in MODULES.values() for call in self.puts(module)]
        self.assertEqual(len(puts), 10)
        for call in puts:
            for value in call.json['data'][0].values():
                self.assertNotIn(value, NO_VALUES)
        self.assertIn({'Translation_Needed': 'YES'}, self.put_fields('FINGERPRINT_SERVICES'))  # no languages

    def test_gaql_target_note_only(self):
        signal = self.signal('apostille')
        record = self.synced(signal, GAQL_MODULE, GET_A_QUOTE_LEADS='Phone Call Received')

        self.run_task()

        self.assertEqual([c for c in self.zoho_writes() if c.method == 'PUT'], [])
        (note,) = self.notes(GAQL_MODULE, record)
        self.assertEqual(note['Note_Title'],
                         f'DCMN · dcmn:form:apostille:{signal.order_id}:translation · suggested')
        self.assertEqual(note['Note_Content'],
                         'Client asked for a certified translation (Spanish → English) on the Apostille form. '
                         'Click Create Translation to create it in Translation Services.')
        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.outcome), ('evaluated', 'suggested'))   # GAQL is an allowed source

    def test_trigger_empty(self):
        for order_type in MODULES:
            self.synced(self.signal(order_type))
        self.synced(self.signal('apostille'), GAQL_MODULE)

        with ONE_CLICK, patch.dict(SOURCE_FIELD_MAPS, FIELD_MAPS):
            self.run_task()

        writes = self.zoho_writes()
        self.assertEqual(len([c for c in writes if c.method == 'PUT']), 5)
        self.assertEqual(len(self.note_writes()), 6)
        for call in writes:
            self.assertEqual(call.json['trigger'], [], call.url)

    def test_transient_error_retries_next_beat(self):
        signal = self.signal('apostille')
        record = self.synced(signal)
        self.crm.fail[('PUT', 'Apostille_Services')] = [requests.Timeout('test timeout')]

        with self.assertLogs('crm', 'WARNING') as logs:
            self.assertIn('retry=1', self.run_task())
        self.assertIn(f'translation_apply_failed signal={signal.pk} code=TIMEOUT retry=True', logs.output[-1])
        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.attempts, signal.last_error), ('waiting_source', 1, 'TIMEOUT'))

        self.run_task()
        signal.refresh_from_db()
        self.assertEqual(signal.state, 'evaluated')
        self.assertEqual(self.crm.records['Apostille_Services'][record['id']]['Translation_Required'],
                         'Yes -Translate')
        self.assertEqual(len(self.notes('Apostille_Services', record)), 1)

    def test_deleted_record_errors_token_failure_retries(self):
        gone = self.signal('apostille')
        ZohoSyncJob.objects.create(order_type='apostille', order_id=gone.order_id, status=ZohoSyncJob.STATUS_SYNCED,
                                   zoho_module='Apostille_Services', zoho_record_id='7100000000000099999')
        fine = self.signal('embassy')
        self.synced(fine)

        with patch('crm.zoho.get_access_token', side_effect=ZohoPermanentError('no token')), \
                self.assertLogs('crm', 'WARNING'):
            self.assertIn('retry=2', self.run_task())
        for signal in (gone, fine):
            signal.refresh_from_db()
            self.assertEqual((signal.state, signal.last_error), ('waiting_source', 'TOKEN_REFRESH'))

        with self.assertLogs('crm', 'WARNING'):
            self.run_task()
        gone.refresh_from_db()
        fine.refresh_from_db()
        self.assertEqual((gone.state, gone.last_error, gone.attempts), ('error', 'record_not_found', 2))
        self.assertEqual(fine.state, 'evaluated')

    def test_flag_off_skips(self):
        self.synced(self.signal('apostille'))
        with override_settings(TRANSLATION_CAPTURE_ENABLED=False):
            self.assertEqual(self.run_task(), 'skipped')
        self.assertNoHttp()


@ONE_CLICK
class PaidFormTests(TranslationTestCase):
    def test_fbi_and_marriage_with_faked_synced_job(self):
        FbiServicePackage.objects.create(code='zz-basic', label='ZZ Basic', price=100)
        ShippingOption.objects.create(code='zz-standard', label='ZZ Standard', price=10)
        answers = {**TICKED, 'translation_source_language': 'English', 'translation_target_language': 'Spanish'}
        orders = {order_type: self.client.post(FORMS[order_type].path, {**FORMS[order_type].body, **answers})
                  for order_type in ('fbi', 'marriage')}
        for response in orders.values():
            self.assertEqual(response.status_code, 201)
        signals = {s.order_type: s for s in ServiceSignal.objects.all()}
        self.assertEqual(set(signals), {'fbi', 'marriage'})

        self.run_task()   # unpaid: no ZohoSyncJob yet
        self.assertNoHttp()

        # Payment: the Stripe webhook enqueues the sync and the legacy task marks the job synced.
        records = {order_type: self.synced(signal) for order_type, signal in signals.items()}
        with patch.dict(SOURCE_FIELD_MAPS, FIELD_MAPS):
            self.assertIn('suggested=2', self.run_task())

        self.assertEqual(self.put_fields('Deals'), [
            {'Translation_Required': 'Yes -Translate', 'Translation_Source_Language': 'English',
             'Translation_Target_Language': 'Spanish', 'Translated_Language': 'Spanish'}])
        self.assertEqual(self.put_fields('Triple_Seal_Apostilles'), [
            {'Translation_Required': True, 'Translation_Source_Language': 'English',
             'Translation_Target_Language': 'Spanish'}])
        for order_type, record in records.items():
            module, order_id = MODULES[order_type], orders[order_type].data['order_id']
            with self.subTest(order_type):
                link = ServiceLink.objects.get(source_key=f'form:{order_type}:{order_id}')
                self.assertEqual((link.state, link.service_key, link.external_key, link.target_module),
                                 ('suggested', 'translation', f'dcmn:form:{order_type}:{order_id}:translation',
                                  'Translation_Services'))
                (note,) = self.notes(module, record)
                label = 'FBI' if order_type == 'fbi' else 'Marriage'
                self.assertEqual(note['Note_Content'],
                                 f'Client asked for a certified translation (English → Spanish) on the {label} form. '
                                 'Click Add translation to create it in Translation Services.')
        self.assertEqual(self.creates('Translation_Services'), [])   # suggested only, never created


class CaptureTests(TranslationTestCase):
    @override_settings(TRANSLATION_CAPTURE_TYPES=['fbi', 'apostille', 'embassy', 'fingerprinting'])
    def test_capture_ignored_counted_per_order_type(self):
        unticked = {key: value for key, value in TICKED.items() if key != 'need_translation'}
        for order_type, extra in (('marriage', TICKED), ('marriage', TICKED), ('marriage', unticked),
                                  ('pre-check', TICKED), ('apostille', TICKED)):
            form = FORMS[order_type]
            self.assertEqual(self.client.post(form.path, {**form.body, **extra}).status_code, 201)

        self.assertEqual(list(ServiceSignal.objects.values_list('order_type', flat=True)), ['apostille'])
        today = timezone.now().astimezone(ET).date()
        self.assertEqual(tc.ignored_counts(today), {'marriage': 2, 'pre-check': 1})
        self.assertEqual(tc.ignored_counts(today - timedelta(days=1)), {})


@ONE_CLICK
class RuleTests(TranslationTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(SOURCE_FIELD_MAPS, FIELD_MAPS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_suggested_link_and_note_once(self):
        signal = self.signal('apostille')
        record = self.synced(signal)
        source_key = f'form:apostille:{signal.order_id}'

        self.run_task()
        self.run_task()
        write_back(source_key)   # the engine's own write-back of the suggested link

        (link,) = ServiceLink.objects.filter(source_key=source_key)
        self.assertEqual((link.state, link.reason, link.trigger, link.caller),
                         ('suggested', 'signal:translation_from_form', 'rule:translation_from_form', 'rules'))
        (note,) = self.notes('Apostille_Services', record)
        self.assertEqual(note['Note_Title'], note_title(link.external_key, 'suggested'))
        self.assertIn('Click Add translation', note['Note_Content'])
        self.assertEqual(len(self.note_writes()), 1)
        self.assertEqual(len(self.puts('Apostille_Services')), 1)
        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.outcome, signal.rule_key, signal.attempts),
                         ('evaluated', 'suggested', 'translation_from_form', 1))
        self.assertEqual(EngineRequest.objects.filter(caller='rules').count(), 1)

    def test_button_key_equals_suggestion_key(self):
        signal = self.signal('apostille')
        record = self.synced(signal)
        self.run_task()
        suggested = ServiceLink.objects.get(source_key=f'form:apostille:{signal.order_id}')

        click = {'mode': 'create', 'trigger': 'zoho_button', 'caller': 'zoho', 'actor': {'kind': 'zoho'}}
        with self.captureOnCommitCallbacks(execute=True):   # a contact lookup is never inline: the task runs
            first = engine.handle(('Apostille_Services', record['id']), ['translation'], **click)
        again = engine.handle(('Apostille_Services', record['id']), ['translation'], **click)

        self.assertEqual([(r['status'], r['link_id']) for r in first.body['results']], [('queued', suggested.pk)])
        self.assertEqual([r['status'] for r in again.body['results']], ['exists'])
        (link,) = ServiceLink.objects.filter(source_key=suggested.source_key)
        self.assertEqual((link.pk, link.state, link.external_key),
                         (suggested.pk, 'created', f'dcmn:form:apostille:{signal.order_id}:translation'))
        (create,) = self.creates('Translation_Services')
        payload = create.json['data'][0]
        self.assertEqual(payload[KEY_FIELD], suggested.external_key)
        self.assertEqual(payload['Languages'], 'Spanish → English')
        self.assertEqual(payload['Translation_Status'], 'Client Placed Request')

    def test_expired_after_30_days(self):
        old = self.signal('fbi')
        fresh = self.signal('fbi')
        ServiceSignal.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        ServiceSignal.objects.filter(pk=fresh.pk).update(expires_at=timezone.now() + timedelta(days=1))

        self.assertEqual(self.run_task(), 'expired=1')

        old.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual((old.state, fresh.state), ('expired', 'waiting_source'))
        self.assertAlmostEqual(tc.capture(TICKED, 'fbi', 1).expires_at, timezone.now() + timedelta(days=30),
                               delta=timedelta(minutes=1))
        self.synced(old)   # paid after the window: nothing is written any more
        self.run_task()
        self.assertNoHttp()
