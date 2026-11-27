"""quotes model constraints and the inert WP1 endpoints (WP1)."""

from django.db import IntegrityError, transaction
from django.test import override_settings

from crm.messages import DISABLED
from crm.models import EngineRequest
from crm.testing import NoNetworkTestCase, make_quote_submission
from quotes.models import QuoteLine, QuoteSubmission
from quotes.views import FALLBACK

ZOHO_KEY = 'k' * 43


class QuoteLineConstraintTests(NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.quote = make_quote_submission()
        QuoteLine.objects.create(quote=self.quote, position=1, service_key='apostille')

    def test_unique_service_per_quote(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            QuoteLine.objects.create(quote=self.quote, position=2, service_key='apostille')

        QuoteLine.objects.create(quote=make_quote_submission(), position=1, service_key='apostille')
        self.assertEqual(QuoteLine.objects.filter(service_key='apostille').count(), 2)

    def test_unique_position(self):
        with transaction.atomic(), self.assertRaises(IntegrityError):
            QuoteLine.objects.create(quote=self.quote, position=1, service_key='translation')

        QuoteLine.objects.create(quote=self.quote, position=2, service_key='translation')
        self.assertEqual(list(self.quote.lines.values_list('service_key', flat=True)), ['apostille', 'translation'])


class QuoteSubmissionTests(NoNetworkTestCase):
    @override_settings(ZOHO_ORG_ID='org882204370', BASE_URL='https://api.example.com')
    def test_links(self):
        quote = make_quote_submission()
        self.assertEqual(quote.zoho_url, '')

        quote.zoho_record_id = '5725767000012345678'
        self.assertEqual(
            quote.zoho_url, 'https://crm.zoho.com/crm/org882204370/tab/CustomModule7/5725767000012345678',
        )
        self.assertEqual(quote.admin_url, f'https://api.example.com/admin/quotes/quotesubmission/{quote.pk}/change/')

    def test_defaults_are_inert(self):
        quote = make_quote_submission()

        self.assertEqual(
            (quote.spam_status, quote.intake_mode, quote.sync_status, quote.staff_email_status,
             quote.confirmation_email_status, quote.zoho_module),
            ('clean', 'none', 'pending', 'pending', 'off', 'Get_A_Quote_Leads'),
        )


class StubEndpointTests(NoNetworkTestCase):
    """With every flag at its default the new endpoints are inert: S0 exit criteria `503`/`401` (DESIGN §13.2)."""

    def test_new_endpoints_return_503_with_defaults(self):
        response = self.client.post('/api/quote-requests/', {'full_name': 'ZZ TEST'}, content_type='application/json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'ok': False, 'detail': 'unavailable', 'fallback': FALLBACK})

        response = self.client.get('/api/quote-requests/config/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual((response.json()['enabled'], response.json()['detail']), (False, 'unavailable'))

        response = self.client.post('/api/quote-requests/uploads/')
        self.assertEqual((response.status_code, response.json()['detail']), (404, 'uploads_disabled'))

        # CRM_ACTION_KEYS is empty by default: every engine API call is 401 (DESIGN §8.1 emergency stop).
        for method, path in (
            ('post', '/api/crm/v1/related-services/'),
            ('get', '/api/crm/v1/related-services/'),
            ('get', '/api/crm/v1/jobs/2f1f4c1e-8f0e-4d55-9f43-6c7a2b1d9e10/'),
            ('post', '/api/crm/v1/panel-links/'),
        ):
            with self.subTest(path=path, method=method):
                response = getattr(self.client, method)(path, {}, content_type='application/json')
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {'ok': False, 'detail': 'unauthorized', 'message': DISABLED})

        # CRM_ENGINE_MODE=off closes the panel whatever the token or login.
        for method, path in (('get', '/crm/panel/'), ('post', '/crm/panel/act/'), ('get', '/crm/panel/zoho/')):
            with self.subTest(path=path, method=method):
                self.assertEqual(getattr(self.client, method)(path).status_code, 503)

        self.assertFalse(QuoteSubmission.objects.exists())
        self.assertFalse(EngineRequest.objects.exists())   # unauthenticated calls are not audited
        self.assertNoHttp()

    @override_settings(CRM_ACTION_KEYS=f'zoho:{ZOHO_KEY}')
    def test_engine_off_answers_disabled_to_a_valid_key(self):
        body = {'api_version': 'rs.v1', 'source': {'module': 'Get_A_Quote_Leads', 'id': '6686316000033717016'},
                'services': ['apostille']}
        response = self.client.post('/api/crm/v1/related-services/', body, content_type='application/json',
                                    HTTP_X_DCMN_KEY=ZOHO_KEY)

        self.assertEqual(response.status_code, 503)
        self.assertEqual((response.json()['ok'], response.json()['detail']), (False, 'disabled'))
        self.assertEqual(response.json()['message'], DISABLED)
        request = EngineRequest.objects.get()   # invariant 10: the refusal is audited
        self.assertEqual((request.caller, request.state, request.http_status), ('zoho', 'refused', 503))
        self.assertNoHttp()
