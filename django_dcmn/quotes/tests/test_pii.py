"""Retention windows and the erase request (DESIGN §4.5, WP4)."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import EngineRequest, ServiceLink, ServiceSignal
from crm.testing import NoNetworkTestCase
from quotes.models import QuoteRejection, QuoteSubmission
from quotes.tasks import purge_quote_pii
from quotes.tasks.maintenance import erase_zoho_record
from quotes.tests.test_pipeline import v5_quote

GAQL_ID = '6686316000044400001'
LEGACY_GAQL_ID = '6686316000033717016'


def aged(quote: QuoteSubmission, days: int) -> QuoteSubmission:
    QuoteSubmission.objects.filter(pk=quote.pk).update(created_at=timezone.now() - timedelta(days=days))
    quote.refresh_from_db()
    return quote


def crm_rows(source_key: str, *, quote=None, record_id: str = '') -> tuple[ServiceLink, EngineRequest, ServiceSignal]:
    link = ServiceLink.objects.create(
        source_kind='quote' if quote else 'zoho', source_key=source_key, source_record_id=record_id, quote=quote,
        service_key='apostille', external_key=f'dcmn:{source_key}:apostille', state='needs_decision',
        actor={'kind': 'zoho', 'email': 'manager@example.com', 'initials': 'AB'},
        candidates=[{'module': 'Apostille_Services', 'id': '9', 'name': 'Apostille · Maria Fernández',
                     'match': 'same_client'}],
    )
    request = EngineRequest.objects.create(
        caller='zoho', mode='create', source_key=source_key, source_record_id=record_id, ip='198.51.100.4',
        actor={'kind': 'zoho', 'email': 'manager@example.com'}, result={'message': 'Created', 'candidates': []},
    )
    signal = ServiceSignal.objects.create(
        dedupe_key=f'translation_requested:{source_key}:translation', kind='translation_requested',
        source_kind=link.source_kind, source_key=source_key, source_record_id=record_id, quote=quote,
        service_key='translation', data={'source_raw': 'Inglés', 'target_raw': 'Español', 'from': 'english'},
    )
    return link, request, signal


class PurgeTests(NoNetworkTestCase):
    def test_ip_ua_nulled_after_90_days(self):
        old = aged(v5_quote(), 91)
        recent = aged(v5_quote(), 89)

        self.assertTrue(purge_quote_pii.apply().get().startswith('ip_ua=1 '))

        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual((old.ip, old.user_agent), (None, ''))
        self.assertEqual((recent.ip, recent.user_agent), ('203.0.113.7', 'Mozilla/5.0 (test)'))
        self.assertIsNotNone(old.raw_payload)                 # the payload has its own window

    def test_raw_payload_nulled_after_365_days(self):
        amendment = {'at': 'x', 'changed': ['message'], 'payload': {'contact': {'message': 'm'}}}
        old = aged(v5_quote(amendments=[amendment]), 366)
        recent = aged(v5_quote(), 300)

        purge_quote_pii.apply()

        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertIsNone(old.raw_payload)
        self.assertEqual(old.amendments, [{'at': 'x', 'changed': ['message'], 'payload': None}])
        self.assertIsNotNone(recent.raw_payload)
        self.assertEqual(old.reference[:2], 'Q-')
        self.assertEqual(old.full_name, 'Maria Fernández')    # the columns stay: only an erase request blanks them

    def test_rejections_deleted_after_30_days(self):
        old = QuoteRejection.objects.create(reason='honeypot')
        QuoteRejection.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        recent = QuoteRejection.objects.create(reason='time_trap')

        purge_quote_pii.apply()

        self.assertEqual(list(QuoteRejection.objects.values_list('pk', flat=True)), [recent.pk])
        self.assertNoHttp()


class EraseTests(NoNetworkTestCase):
    def test_erase_keeps_reference_and_line_keys(self):
        amendment = {'at': 'x', 'changed': ['contact.phone'], 'payload': {'contact': {'phone': '+1'}}}
        quote = v5_quote(amendments=[amendment])
        keys = list(quote.lines.order_by('position').values_list('position', 'service_key'))

        call_command('quotes_erase', '--reference', quote.reference, stdout=StringIO())

        quote.refresh_from_db()
        self.assertEqual((quote.full_name, quote.email, quote.email_normalized, quote.phone_raw, quote.phone_e164,
                          quote.phone_last10, quote.message, quote.ip, quote.user_agent),
                         ('', '', '', '', '', '', '', None, ''))
        self.assertIsNone(quote.raw_payload)
        self.assertEqual(quote.amendments[0]['payload'], None)
        self.assertIsNotNone(quote.pii_erased_at)
        self.assertEqual(list(quote.lines.order_by('position').values_list('position', 'service_key')), keys)
        self.assertTrue(all(line.answers_raw == {} for line in quote.lines.all()))
        self.assertRegex(quote.reference, r'^Q-\d{6}$')

        with self.assertRaises(CommandError):
            call_command('quotes_erase', '--reference', 'Q-000000', stdout=StringIO())

    def test_quotes_erase_covers_crm_rows(self):
        quote = v5_quote(zoho_record_id=GAQL_ID)
        own = crm_rows(f'qr:{quote.pk}', quote=quote)
        by_record = crm_rows(f'rel:gaql:{GAQL_ID}', record_id=GAQL_ID)
        other = crm_rows('qr:999999')

        call_command('quotes_erase', '--reference', quote.reference, stdout=StringIO())

        for link, request, signal in (own, by_record):
            link.refresh_from_db()
            request.refresh_from_db()
            signal.refresh_from_db()
            self.assertEqual(link.candidates, [{'module': 'Apostille_Services', 'id': '9', 'name': '',
                                                'match': 'same_client'}])
            self.assertEqual(link.actor, {'kind': 'zoho', 'email': '', 'initials': 'AB'})
            self.assertEqual((request.result, request.actor, request.ip), (None, {}, None))
            self.assertEqual(signal.data, {'from': 'english'})
        link, request, signal = other
        link.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(link.actor['email'], 'manager@example.com')
        self.assertEqual(request.ip, '198.51.100.4')

    def test_erase_by_zoho_record_for_legacy_source(self):
        link, request, signal = crm_rows(f'rel:gaql:{LEGACY_GAQL_ID}', record_id=LEGACY_GAQL_ID)
        quote = v5_quote()

        out = StringIO()
        call_command('quotes_erase', '--zoho-record', LEGACY_GAQL_ID, stdout=out)

        self.assertIn('erased 3 row(s)', out.getvalue())
        link.refresh_from_db()
        request.refresh_from_db()
        signal.refresh_from_db()
        self.assertEqual(link.candidates[0]['name'], '')
        self.assertEqual((request.result, request.ip), (None, None))
        self.assertNotIn('source_raw', signal.data)
        quote.refresh_from_db()
        self.assertEqual(quote.full_name, 'Maria Fernández')    # an unrelated quote is untouched

        synced = v5_quote(zoho_record_id=GAQL_ID)
        self.assertEqual(erase_zoho_record(GAQL_ID), 1)          # a v3 GAQL id erases that quote
        synced.refresh_from_db()
        self.assertIsNotNone(synced.pii_erased_at)
        with self.assertRaises(CommandError):
            call_command('quotes_erase', '--zoho-record', '6686316000000000000', stdout=StringIO())
