"""WP7 capture (DESIGN §10.1, §10.2): the legacy forms answer byte for byte as before, and a ticked
`need_translation` becomes exactly one `waiting_source` ServiceSignal.

The legacy views run for real (orders, tracking, staff and client emails to the locmem backend); the Zoho
sync they enqueue waits for `on_commit`, which never fires inside a TestCase, so nothing reaches a network.
"""

from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.core.management.color import no_style
from django.db import connection, transaction
from django.test import override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from crm import translation_capture as tc
from crm.models import ServiceSignal
from crm.testing import NoNetworkTestCase
from orders.models import FbiServicePackage, ShippingOption
from orders.views.orders import (
    CreateApostilleOrderView,
    CreateEmbassyOrderView,
    CreateFbiOrderView,
    CreateFingerprintingView,
    CreateMarriageOrderView,
    CreatePreCheckView,
    CreateQuoteRequestView,
    CreateTranslationOrderView,
)

CLIENT = {'name': 'ZZ TEST Translation Client', 'email': 'zz.translation@example.com', 'phone': '2025550100'}
TICKED = {'need_translation': 'yes', 'translation_source_language': 'Spanish',
          'translation_target_language': 'English'}


@dataclass(frozen=True)
class Form:
    path: str
    name: str
    view: type
    legacy: type
    body: dict


FORMS = {
    'fbi': Form('/api/fbi/create-order/', 'fbi_create_order', tc.FbiOrderWithTranslation, CreateFbiOrderView,
                {**CLIENT, 'country_name': 'Spain', 'address': '1 Test St, Washington, DC', 'package': 'zz-basic',
                 'count': '2', 'shipping_option': 'zz-standard'}),
    'marriage': Form('/api/marriage/create-order/', 'marriage_create_order', tc.MarriageOrderWithTranslation,
                     CreateMarriageOrderView, {**CLIENT, 'address': '1 Test St, Washington, DC'}),
    'embassy': Form('/api/embassy/create-order/', 'create-embassy-order', tc.EmbassyOrderWithTranslation,
                    CreateEmbassyOrderView, {**CLIENT, 'address': '1 Test St, Washington, DC',
                                             'document_type': 'Diploma', 'country': 'Spain'}),
    'apostille': Form('/api/apostille/create-order/', 'create-apostille-order', tc.ApostilleOrderWithTranslation,
                      CreateApostilleOrderView, {**CLIENT, 'type': 'Diploma', 'country': 'Spain',
                                                 'service_type': 'Office'}),
    'pre-check': Form('/api/pre-check/submit/', 'pre-check-submit', tc.PreCheckWithTranslation, CreatePreCheckView,
                      {**CLIENT, 'document_type': 'Diploma', 'destination_country': 'Spain'}),
    'fingerprinting': Form('/api/fingerprinting/submit/', 'fingerprinting-submit', tc.FingerprintingWithTranslation,
                           CreateFingerprintingView, {**CLIENT, 'preferred_date': '2026-12-01',
                                                      'preferred_time': '10:00', 'service_location': 'Office'}),
}


class FormTestCase(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        FbiServicePackage.objects.create(code='zz-basic', label='ZZ Basic', price=100)
        ShippingOption.objects.create(code='zz-standard', label='ZZ Standard', price=10)

    def post(self, form: Form, extra: dict | None = None):
        return self.client.post(form.path, {**form.body, **(extra or {})})

    def post_rolled_back(self, form: Form, extra: dict | None = None):
        """POST inside a rolled-back savepoint, so the next POST gets the same order id.

        SQLite rolls its id counter back with the savepoint; Postgres sequences are first reset to max(id).
        """
        reset = connection.ops.sequence_reset_sql(no_style(), apps.get_app_config('orders').get_models())
        with connection.cursor() as cursor:
            for sql in reset:
                cursor.execute(sql)
        with transaction.atomic():
            response = self.post(form, extra)
            transaction.set_rollback(True)
        return response


@patch('orders.services.tracking.generate_tid', return_value='ZZTESTTID1')
class PassThroughTests(FormTestCase):
    def assertSameResponse(self, on, off):
        self.assertEqual(off.status_code, 201, off.content)
        self.assertEqual(on.status_code, off.status_code)
        self.assertEqual(on.content, off.content)
        self.assertEqual(on.data, off.data)
        self.assertEqual(dict(on.headers), dict(off.headers))

    def test_response_byte_identical_flag_on_and_off(self, _tid):
        for order_type, form in FORMS.items():
            with self.subTest(order_type):
                with override_settings(TRANSLATION_CAPTURE_ENABLED=False):
                    off = self.post_rolled_back(form, TICKED)
                with override_settings(TRANSLATION_CAPTURE_ENABLED=True), \
                        patch.object(tc, 'capture', wraps=tc.capture) as capture:
                    on = self.post_rolled_back(form, TICKED)
                self.assertSameResponse(on, off)
                capture.assert_called_once()
                self.assertEqual(capture.call_args.args[1:], (order_type, on.data['order_id']))

    def test_capture_exception_swallowed_response_unchanged(self, _tid):
        for order_type, form in FORMS.items():
            with self.subTest(order_type):
                off = self.post_rolled_back(form, TICKED)
                with override_settings(TRANSLATION_CAPTURE_ENABLED=True), \
                        patch.object(tc, 'capture', side_effect=RuntimeError('boom')), \
                        self.assertLogs('crm.translation_capture', 'ERROR') as logs:
                    on = self.post_rolled_back(form, TICKED)
                self.assertSameResponse(on, off)
                self.assertIn(f'translation_capture_failed order_type={order_type}', logs.output[0])
        self.assertFalse(ServiceSignal.objects.exists())


class RoutingTests(NoNetworkTestCase):
    def test_paths_resolve_to_subclasses(self):
        for order_type, form in FORMS.items():
            with self.subTest(order_type):
                view_class = resolve(form.path).func.view_class
                self.assertIs(view_class, form.view)
                self.assertTrue(issubclass(view_class, form.legacy))
                self.assertEqual(view_class.order_type, order_type)
        # The other legacy forms keep their legacy views.
        self.assertIs(resolve('/api/translation/create-order/').func.view_class, CreateTranslationOrderView)
        self.assertIs(resolve('/api/quote/create-order/').func.view_class, CreateQuoteRequestView)

    def test_reverse_names_unchanged(self):
        for order_type, form in FORMS.items():
            with self.subTest(order_type):
                self.assertEqual(reverse(form.name), form.path)
        self.assertEqual(reverse('create-translation-order'), '/api/translation/create-order/')


class ParseTests(FormTestCase):
    def test_truthy_variants(self):
        for value in ('yes', 'YES', ' Yes ', 'on', 'true', 'True', '1', True, 1):
            with self.subTest(value=value):
                self.assertIsNotNone(tc.parse_translation({'need_translation': value}))
        for value in ('no', '', 'off', 'false', '0', 'y', 'checked', False, 0, None, ['yes']):
            with self.subTest(value=value):
                self.assertIsNone(tc.parse_translation({'need_translation': value}))
        self.assertIsNone(tc.parse_translation({'translation_source_language': 'Spanish'}))

    def test_languages_normalised_raw_kept(self):
        cases = {
            ('Spanish', 'English'): ('Spanish', 'English'),          # what the forms send
            ('español', 'Inglés'): ('Spanish', 'English'),           # ES names, any case and accents
            ('portuguese', 'FRENCH'): ('Portuguese', 'French'),      # slugs and EN names
            ('Other language', '  Haitian   Creole '): ('Other language', 'Haitian Creole'),
            ('Klingon', ''): ('Klingon', ''),                        # unknown: kept as typed; missing: ''
        }
        for (source, target), (lang_from, lang_to) in cases.items():
            with self.subTest(source=source, target=target):
                parsed = tc.parse_translation({'need_translation': 'yes', 'translation_source_language': source,
                                               'translation_target_language': target})
                self.assertEqual(parsed, {'lang_from': lang_from, 'lang_from_raw': ' '.join(source.split()),
                                          'lang_to': lang_to, 'lang_to_raw': ' '.join(target.split())})
        long = tc.parse_translation({'need_translation': 'on', 'translation_source_language': 'x' * 90,
                                     'translation_target_language': ['Spanish']})
        self.assertEqual((len(long['lang_from']), len(long['lang_from_raw'])), (60, 60))
        self.assertEqual((long['lang_to'], long['lang_to_raw']), ('', ''))

    @override_settings(TRANSLATION_CAPTURE_ENABLED=True)
    def test_unticked_writes_nothing(self):
        languages = {key: value for key, value in TICKED.items() if key != 'need_translation'}
        for order_type, form in FORMS.items():
            for extra in (languages, {**languages, 'need_translation': 'no'}, {}):
                with self.subTest(order_type, extra=extra):
                    self.assertEqual(self.post(form, extra).status_code, 201)
        self.assertFalse(ServiceSignal.objects.exists())
        self.assertEqual(tc.ignored_counts(timezone.now().date()), {})


@override_settings(TRANSLATION_CAPTURE_ENABLED=True)
class SignalTests(FormTestCase):
    def test_waiting_source_signal_created_once(self):
        response = self.post(FORMS['apostille'], {**TICKED, 'translation_source_language': 'español'})
        order_id = response.data['order_id']

        again = tc.capture({**TICKED, 'translation_target_language': 'French'}, 'apostille', order_id)

        signal = ServiceSignal.objects.get()
        self.assertEqual(again.pk, signal.pk)
        self.assertEqual(
            (signal.dedupe_key, signal.kind, signal.source_kind, signal.source_key, signal.order_type,
             signal.order_id, signal.service_key, signal.state, signal.source_module, signal.source_record_id),
            (f'translation_requested:form:apostille:{order_id}:translation', 'translation_requested', 'form',
             f'form:apostille:{order_id}', 'apostille', order_id, 'translation', 'waiting_source', '', ''))
        self.assertEqual(signal.data, {'lang_from': 'Spanish', 'lang_from_raw': 'español',
                                       'lang_to': 'English', 'lang_to_raw': 'English'})   # the first answer wins
        self.assertAlmostEqual(signal.expires_at, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))

        self.post(FORMS['fbi'], TICKED)
        self.assertEqual(sorted(ServiceSignal.objects.values_list('order_type', flat=True)), ['apostille', 'fbi'])
