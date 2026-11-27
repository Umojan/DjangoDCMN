"""Auto-intake of a v5 quote (DESIGN §7.3): primary line only, suspect quotes noted, candidates decide, implied
apostille suggested."""

from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from crm.intake import start_intake
from crm.models import ServiceLink, ServiceSignal
from crm.tests.support import GAQL_ID, EngineTestCase, saved_quote
from quotes import flags as quote_flags
from quotes.models import GAQL_MODULE


class IntakeTests(EngineTestCase):
    def test_primary_only_pending_link(self):
        quote = saved_quote('v5_success', intake_mode='primary',
                            body_changes=lambda body: body['contact'].update(full_name='ZZ TEST Maria'))

        with self.captureOnCommitCallbacks() as callbacks:
            ids = start_intake(quote.pk)

        link = ServiceLink.objects.get()
        self.assertEqual(ids, [link.pk])
        self.assertEqual((link.service_key, link.state, link.reason, link.trigger, link.caller),
                         ('apostille', 'pending', 'intake', 'intake', 'system'))
        self.assertEqual(link.request.client_request_id, f'qr:{quote.pk}:intake')
        self.assertTrue(callbacks)   # the link task is queued, never run inline
        self.assertNoHttp()
        self.assertEqual(start_intake(quote.pk), [link.pk])   # idempotent
        self.assertEqual(ServiceLink.objects.count(), 1)

    def test_default_mode_is_none(self):
        with override_settings():
            del settings.QUOTE_AUTO_INTAKE
            self.assertEqual(quote_flags.auto_intake(), 'none')
            self.assertEqual(quote_flags.intake_mode_snapshot(), 'none')
            self.assertEqual(saved_quote().intake_mode, 'none')

    def test_mode_none_creates_nothing(self):
        quote = saved_quote(intake_mode='none')

        self.assertEqual(start_intake(quote.pk), [])

        self.assertFalse(ServiceLink.objects.exists())
        self.assertNoHttp()

    def test_snapshot_none_when_engine_not_live(self):
        with override_settings(QUOTE_AUTO_INTAKE='primary'):
            for mode, expected in (('off', 'none'), ('dry_run', 'none'), ('live', 'primary')):
                with self.subTest(engine=mode), override_settings(CRM_ENGINE_MODE=mode):
                    self.assertEqual(quote_flags.intake_mode_snapshot(), expected)
                    self.assertEqual(saved_quote().intake_mode, expected)

    def test_suspect_quote_skips_intake_with_note(self):
        quote = saved_quote(intake_mode='primary', spam_status='suspect', spam_reasons=['turnstile_failed'])
        self.gaql_record(quote)

        self.assertEqual(start_intake(quote.pk), [])
        self.assertEqual(start_intake(quote.pk), [])

        self.assertFalse(ServiceLink.objects.exists())
        (note,) = self.crm.notes[(GAQL_MODULE, GAQL_ID)]
        self.assertEqual(note['Note_Title'], f'DCMN · qr:{quote.pk} · possible spam')
        self.assertIn('nothing was created automatically', note['Note_Content'])
        self.assertEqual([c.url.rsplit('/', 1)[-1] for c in self.zoho_writes()], ['Notes'])

    def test_no_target_primary_returns_empty(self):
        for fixture in ('svc_retrieval', 'svc_other'):
            with self.subTest(fixture=fixture):
                self.assertEqual(start_intake(saved_quote(fixture, intake_mode='primary').pk), [])
        self.assertFalse(ServiceLink.objects.exists())
        self.assertNoHttp()

    def test_candidate_makes_needs_decision_note_no_create(self):
        quote = saved_quote(intake_mode='primary')
        self.gaql_record(quote)
        open_record = self.crm.add('Apostille_Services', Name='Apostille · by hand', Client_Email=quote.email,
                                   Status='Initial Contact Made - SEND BY GMAIL',
                                   Created_Time=(timezone.now() - timedelta(days=3)).isoformat())

        with self.captureOnCommitCallbacks(execute=True):
            start_intake(quote.pk)

        link = ServiceLink.objects.get()
        self.assertEqual(link.state, 'needs_decision')
        self.assertEqual([c['id'] for c in link.candidates], [open_record['id']])
        self.assertEqual(self.creates('Apostille_Services'), [])
        (note,) = self.crm.notes[(GAQL_MODULE, GAQL_ID)]
        self.assertEqual(note['Note_Title'], f'DCMN · {link.external_key} · needs decision')
        self.assertTrue(note['Note_Content'].startswith(
            'Not created automatically: this client already has an open Apostille record '
            f'https://crm.zoho.com/crm/org882204370/tab/CustomModule2/{open_record["id"]}.'))
        self.assertIn('Link it or create new in Add service…', note['Note_Content'])

    def test_implied_apostille_suggested(self):
        quote = saved_quote('svc_fbi')   # fbi with apostille=yes implies an apostille

        with override_settings(CRM_ENGINE_MODE='off'):
            self.assertEqual(start_intake(quote.pk), [])
        signal = ServiceSignal.objects.get()
        self.assertEqual((signal.kind, signal.service_key, signal.state), ('apostille_implied', 'apostille', 'new'))
        self.assertFalse(ServiceLink.objects.exists())

        start_intake(quote.pk)

        signal.refresh_from_db()
        self.assertEqual((signal.state, signal.outcome), ('evaluated', 'suggested'))
        link = ServiceLink.objects.get()
        self.assertEqual((link.service_key, link.state), ('apostille', 'suggested'))
        self.assertEqual(ServiceSignal.objects.count(), 1)
        self.assertNoHttp()
