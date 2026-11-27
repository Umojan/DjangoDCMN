"""Silent Tracking IDs (DESIGN §7.12): Track row before the create, TID in the payload, no client email by default."""

from unittest.mock import patch

from django.test import override_settings

from crm import engine, sources, tid
from crm.facts import from_quote
from crm.registry import REGISTRY
from crm.testing import FakeResponse
from crm.tests.support import EngineTestCase, make_link, saved_quote
from orders.models import Track
from quotes.models import QuoteSubmission

TID_SERVICES = {'apostille': 'state_apostille', 'embassy': 'embassy_legalization', 'translation': 'translation',
                'fbi-apostille-deal': 'fbi_apostille'}


@patch('crm.tid.send_tracking_email_task')
class TidTests(EngineTestCase):
    def created(self, quote: QuoteSubmission, service: str = 'apostille'):
        link = make_link(quote, service)
        self.assertEqual(engine.run_link(link.pk), 'created')
        link.refresh_from_db()
        return link

    def payload(self, module: str) -> dict:
        return self.creates(module)[-1].json['data'][0]

    def test_only_policy_modules(self, email_task):
        quote = saved_quote('v5_success')
        for key, spec in REGISTRY.items():
            with self.subTest(service=key):
                link = make_link(quote, key)
                minted = tid.mint_silent(spec.module, key, from_quote(quote, None), link)
                self.assertEqual(bool(minted), key in TID_SERVICES)
                if minted:
                    self.assertEqual(Track.objects.get(tid=minted).service, TID_SERVICES[key])
        self.assertEqual(Track.objects.count(), len(TID_SERVICES))

        with override_settings(ENGINE_TID_POLICY={'Apostille_Services': 'off'}):
            other = saved_quote('svc_apostille')
            self.assertIsNone(tid.mint_silent('Apostille_Services', 'apostille', from_quote(other, None),
                                              make_link(other, 'apostille')))
        # Modules without a policy entry (Notary, I-9, FINGERPRINT, Triple Seal) never get a Tracking_ID.
        notary = self.created(saved_quote('svc_mobile-notary'), 'mobile-notary')
        self.assertEqual(notary.tracking_id, '')
        self.assertNotIn('Tracking_ID', self.payload('Notary_Services'))

    def test_reuses_tid_on_retry(self, email_task):
        quote = saved_quote()
        link = make_link(quote, 'apostille')
        self.crm.fail[('POST', 'Apostille_Services')] = [FakeResponse(500, {'code': 'INTERNAL_ERROR'})]

        self.assertEqual(engine.run_link(link.pk), 'retry')
        link.refresh_from_db()
        first = link.tracking_id
        self.assertTrue(first)
        link.next_attempt_at = None
        link.save()
        self.assertEqual(engine.run_link(link.pk), 'created')

        first_post, second_post = self.creates('Apostille_Services')
        self.assertEqual(first_post.json['data'][0]['Tracking_ID'], first)
        self.assertEqual(second_post.json['data'][0]['Tracking_ID'], first)
        self.assertEqual(Track.objects.count(), 1)

    def test_never_queues_tracking_email_by_default(self, email_task):
        quote = saved_quote('v5_success', confirmation_email_status='off')

        link = self.created(quote)

        self.assertTrue(link.tracking_id)
        email_task.delay.assert_not_called()
        quote.refresh_from_db()
        self.assertEqual(quote.created_email_tid, '')

    @override_settings(ENGINE_TID_CREATED_EMAIL='first_per_quote')
    def test_created_email_first_per_quote_only_when_confirmation_off(self, email_task):
        for status in ('pending', 'sending', 'sent', 'failed'):
            with self.subTest(confirmation=status):
                self.created(saved_quote('v5_success', confirmation_email_status=status))
                email_task.delay.assert_not_called()

        for status in ('off', 'skipped'):
            with self.subTest(confirmation=status):
                email_task.reset_mock()
                link = self.created(saved_quote('v5_success', confirmation_email_status=status))
                email_task.delay.assert_called_once_with(link.tracking_id, 'created')

    @override_settings(ENGINE_TID_CREATED_EMAIL='first_per_quote')
    def test_created_email_never_twice_per_quote(self, email_task):
        quote = saved_quote('v5_success')

        first = self.created(quote, 'apostille')
        self.created(quote, 'translation')
        self.assertFalse(tid.maybe_send_created_email(first))   # a retry of the first link

        email_task.delay.assert_called_once_with(first.tracking_id, 'created')
        quote.refresh_from_db()
        self.assertEqual(quote.created_email_tid, first.tracking_id)

    @override_settings(ENGINE_TID_CREATED_EMAIL='first_per_quote')
    def test_created_email_not_for_test_quote(self, email_task):
        self.created(saved_quote('svc_apostille'))
        email_task.delay.assert_not_called()

        with override_settings(QUOTE_V3_TEST_EMAILS=True):
            link = self.created(saved_quote('svc_apostille'))
        email_task.delay.assert_called_once_with(link.tracking_id, 'created')

    def test_track_row_deleted_on_permanent_failure(self, email_task):
        link = make_link(saved_quote(), 'apostille')
        self.crm.fail[('POST', 'Apostille_Services')] = [FakeResponse(400, {'data': [{
            'code': 'INVALID_DATA', 'status': 'error', 'details': {'api_name': 'Country_of_Use'}}]})]

        self.assertEqual(engine.run_link(link.pk), 'failed')

        link.refresh_from_db()
        self.assertEqual((link.last_error_code, link.tracking_id), ('INVALID_DATA', ''))
        self.assertEqual(Track.objects.count(), 0)

    def test_track_row_kept_for_target_deleted(self, email_task):
        quote = saved_quote()
        link = self.created(quote)
        del self.crm.records['Apostille_Services'][link.target_record_id]

        result = engine.handle(sources.for_quote(quote), ['apostille'], mode='create', trigger='zoho_button',
                               caller='zoho', actor={})

        self.assertEqual(result.body['results'][0]['status'], 'target_deleted')
        link.refresh_from_db()
        self.assertEqual(link.state, 'target_deleted')
        tid.void_orphan_track(link)   # a record was made once: its tracking page stays
        self.assertTrue(Track.objects.filter(tid=link.tracking_id).exists())
        self.assertTrue(link.tracking_id)

    def test_track_data_fields(self, email_task):
        quote = saved_quote('v5_success')

        link = self.created(quote)

        track = Track.objects.get(tid=link.tracking_id)
        self.assertEqual(track.service, 'state_apostille')
        self.assertEqual(track.data, {
            'name': 'Maria Fernández', 'email': 'maria@example.com', 'service': 'state_apostille',
            'current_stage': 'document_received', 'zoho_module': 'Apostille_Services',
            'record_id': link.target_record_id, 'origin': 'crm', 'source_key': f'qr:{quote.pk}'})
        self.assertEqual(self.payload('Apostille_Services')['Tracking_ID'], link.tracking_id)
        # An apostille made from a Deal is an FBI apostille.
        self.assertEqual(tid.tid_service('apostille', 'zoho', 'rel:deal:6686316000012345678'), 'fbi_apostille')
        self.assertEqual(tid.tid_service('apostille', 'quote', 'qr:1'), 'state_apostille')
        self.assertIsNone(tid.tid_service('i9', 'quote', 'qr:1'))
