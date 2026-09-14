"""Tests for the Business Account / Partner application endpoints.

Run:  DJANGO_SETTINGS_MODULE=django_dcmn.settings_test python3 manage.py test orders.test_applications
"""
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import Application, ZohoSyncJob
from .services.applications import build_questionnaire, build_notification_subject
from .zoho_sync import build_application_lead_payload, sync_application_to_zoho


BA_PAYLOAD = {
    "program": "business_account",
    "contact_name": "Jane Doe",
    "organization": "Acme LLP",
    "email": "Jane@Acme.com",
    "phone": "+1 202 555 0100",
    "role": "Paralegal",
    "city_state": "Boston, MA",
    "org_type": "Law firm",
    "services": ["federal_apostille", "i9", "livescan"],
    "order_frequency": "Weekly or more — I want a Corporate Account",
    "countries": "Spain, UAE",
    "notes": "We need weekly pickups.",
    "source_page": "/business-accounts",
    "page_url": "https://www.dcmobilenotary.com/business-accounts?utm_source=google",
    "attribution": {"source": "google", "medium": "cpc", "campaign": "b2b", "gclid": "abc123"},
    "unknown_key": "ignored",
}

PARTNER_PAYLOAD = {
    "program": "partner",
    "contact_name": "Carlos",
    "company": "Docs Express",
    "email": "carlos@docs.example",
    "phone": "+34 600 000 000",
    "website": "https://docs.example",
    "state_country": "Florida / Spain",
    "business_type": "Apostille / document service",
    "services": ["federal_apostille", "embassy", "other"],
    "monthly_volume": "16-40",
    "start_timing": "This month",
    "countries": "Spain",
    "delivery_preference": "Ship directly to my client, unbranded",
    "notes": "",
    "partner_terms_ack": True,
    "source_page": "/partners",
}


class ApplicationEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.ba_url = reverse('business-account-apply')
        self.partner_url = reverse('partner-apply')
        publish = patch('orders.tasks.sync_order_to_zoho_task.delay')
        self.publish = publish.start()
        self.addCleanup(publish.stop)

    def test_business_account_success_saves_record_queues_zoho_and_emails_managers(self):
        resp = self.client.post(self.ba_url, BA_PAYLOAD, format='json', HTTP_ORIGIN='https://www.dcmobilenotary.com')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['ok'])
        app = Application.objects.get(id=resp.json()['id'])

        self.assertEqual(app.program, Application.PROGRAM_BUSINESS_ACCOUNT)
        self.assertEqual(app.organization, 'Acme LLP')
        self.assertEqual(app.email, 'jane@acme.com')
        self.assertEqual(app.location, 'Boston, MA')
        self.assertEqual(app.org_type, 'Law firm')
        self.assertEqual(app.volume, 'Weekly or more — I want a Corporate Account')
        self.assertEqual(app.services, ['federal_apostille', 'i9', 'livescan'])
        self.assertEqual(app.attribution_data['source'], 'google')
        self.assertEqual(app.attribution_data['lead_type'], 'form')
        self.assertEqual(app.raw_payload['unknown_key'], 'ignored')
        self.assertFalse(app.zoho_synced)

        # Durable outbox intent + Celery publish (same path as the other forms)
        job = ZohoSyncJob.objects.get(order_type='application', order_id=app.id)
        self.assertEqual(job.status, ZohoSyncJob.STATUS_PENDING)

        # Manager email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.subject, 'New Business Account application — Acme LLP (Law firm)')
        self.assertEqual(email.to, ['jules@example.com', 'daveys@example.com'])
        self.assertIn('Services: Federal apostille', email.body)
        self.assertIn('/admin/orders/application/%s/change/' % app.id, email.body)
        self.assertIn('Source: google', email.body)
        self.assertEqual(email.reply_to, ['jane@acme.com'])
        app.refresh_from_db()
        self.assertTrue(app.email_sent)

        # CORS header for the site origin
        self.assertEqual(resp['Access-Control-Allow-Origin'], 'https://www.dcmobilenotary.com')

    def test_partner_success(self):
        resp = self.client.post(self.partner_url, PARTNER_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        app = Application.objects.get(id=resp.json()['id'])
        self.assertEqual(app.program, Application.PROGRAM_PARTNER)
        self.assertEqual(app.organization, 'Docs Express')
        self.assertEqual(app.location, 'Florida / Spain')
        self.assertEqual(app.org_type, 'Apostille / document service')
        self.assertEqual(app.volume, '16-40')
        self.assertTrue(app.partner_terms_ack)
        self.assertEqual(mail.outbox[0].subject, 'New Partner application — Docs Express (Florida / Spain)')

    def test_partner_requires_terms_ack(self):
        payload = {**PARTNER_PAYLOAD, 'partner_terms_ack': False}
        resp = self.client.post(self.partner_url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {'detail': 'Please confirm the partner terms to continue.'})
        self.assertEqual(Application.objects.count(), 0)

    def test_partner_requires_company(self):
        payload = {**PARTNER_PAYLOAD, 'company': ''}
        resp = self.client.post(self.partner_url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['detail'], 'Please enter your company name.')

    def test_business_account_requires_organization(self):
        payload = {k: v for k, v in BA_PAYLOAD.items() if k != 'organization'}
        resp = self.client.post(self.ba_url, payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['detail'], 'Please enter your organization name.')

    def test_invalid_email_and_missing_fields_return_human_detail(self):
        resp = self.client.post(self.partner_url, {"program": "partner", "contact_name": "Test", "email": "bad",
                                                   "phone": "1", "company": "X", "partner_terms_ack": False}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['detail'], 'Please enter a valid email address.')

        resp = self.client.post(self.ba_url, {"program": "business_account"}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('detail', resp.json())
        self.assertIsInstance(resp.json()['detail'], str)

    def test_notes_too_long(self):
        resp = self.client.post(self.ba_url, {**BA_PAYLOAD, 'notes': 'x' * 5001}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['detail'], 'Notes are too long (5000 characters max).')

    def test_honeypot_returns_ok_without_saving(self):
        resp = self.client.post(self.ba_url, {**BA_PAYLOAD, 'fax': '555'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_zoho_or_email_failure_does_not_break_response(self):
        with patch('orders.views.applications.enqueue_zoho_sync', side_effect=RuntimeError('boom')), \
             patch('orders.services.applications.EmailMessage.send', side_effect=RuntimeError('mail down')):
            resp = self.client.post(self.ba_url, BA_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, 200)
        app = Application.objects.get(id=resp.json()['id'])
        self.assertFalse(app.email_sent)
        # outbox intent still exists thanks to the post_save signal
        self.assertTrue(ZohoSyncJob.objects.filter(order_type='application', order_id=app.id).exists())

    @override_settings(TURNSTILE_SECRET_KEY='secret')
    def test_turnstile_invalid_token_rejected_but_missing_token_allowed(self):
        class FakeResp:
            def json(self):
                return {'success': False, 'error-codes': ['invalid-input-response']}

        with patch('orders.services.applications.requests.post', return_value=FakeResp()) as post:
            resp = self.client.post(self.ba_url, {**BA_PAYLOAD, 'turnstile_token': 'bad'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['detail'], 'Verification failed, please reload the page and try again.')
        post.assert_called_once()

        resp = self.client.post(self.ba_url, BA_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.ba_url).status_code, 405)

    def test_throttled_returns_detail(self):
        from .views.applications import ApplicationBurstThrottle
        with patch.object(ApplicationBurstThrottle, 'THROTTLE_RATES', {'applications_burst': '2/min', 'applications_sustained': '20/hour'}):
            for _ in range(2):
                self.assertEqual(self.client.post(self.partner_url, PARTNER_PAYLOAD, format='json').status_code, 200)
            resp = self.client.post(self.partner_url, PARTNER_PAYLOAD, format='json')
        self.assertEqual(resp.status_code, 429)
        self.assertIn('detail', resp.json())


class ApplicationZohoTests(TestCase):
    def make_app(self, **overrides):
        data = dict(
            program=Application.PROGRAM_PARTNER, contact_name='Ana Maria Lopez', email='ana@docs.example',
            phone='+1 305 555 0101', organization='Docs Express', website='https://docs.example',
            location='Florida / Spain', org_type='Apostille / document service',
            services=['federal_apostille', 'other'], volume='16-40', start_timing='This month',
            countries='Spain', delivery_preference='Ship directly to my client, unbranded',
            notes='Hello', partner_terms_ack=True, source_page='/partners',
            attribution_data={'source': 'google', 'medium': 'cpc'},
        )
        data.update(overrides)
        return Application.objects.create(**data)

    def test_questionnaire_and_payload(self):
        app = self.make_app()
        text = build_questionnaire(app)
        self.assertTrue(text.startswith('[PARTNER APPLICATION]'))
        self.assertIn('Company: Docs Express', text)
        self.assertIn('Services: Federal apostille (U.S. Dept. of State), Other', text)
        self.assertIn('Partner terms accepted: yes', text)
        self.assertIn('Source: google', text)

        with override_settings(ZOHO_APPLICATIONS_OWNER_IDS=['111', '222']):
            record = build_application_lead_payload(app, duplicates=[{'id': '9', 'Company': 'Old Co', 'Last_Name': 'Lopez', 'Created_Time': '2026-01-01T00:00:00'}])
        self.assertEqual(record['First_Name'], 'Ana Maria')
        self.assertEqual(record['Last_Name'], 'Lopez')
        self.assertEqual(record['Company'], 'Docs Express')
        self.assertEqual(record['Lead_Source'], 'Partner')
        self.assertEqual(record['Lead_Status'], 'Not Contacted')
        self.assertNotIn('Tag', record)
        self.assertEqual(record['Website'], 'https://docs.example')
        self.assertEqual(record['Owner'], {'id': ['111', '222'][app.id % 2]})
        self.assertTrue(record['Description'].startswith('⚠ duplicate email'))

        ba = self.make_app(program=Application.PROGRAM_BUSINESS_ACCOUNT, contact_name='Prince', role='Paralegal', website='')
        record = build_application_lead_payload(ba)
        self.assertNotIn('First_Name', record)
        self.assertEqual(record['Last_Name'], 'Prince')
        self.assertNotIn('Lead_Source', record)
        self.assertEqual(record['Designation'], 'Paralegal')
        self.assertNotIn('Owner', record)
        self.assertEqual(build_notification_subject(ba), 'New Business Account application — Docs Express (Apostille / document service)')

    @patch('orders.zoho_sync.add_tags_to_record', return_value=True)
    @patch('orders.zoho_sync._find_leads_by_email', return_value=[])
    @patch('orders.zoho_sync.module_has_field', return_value=True)
    @patch('orders.zoho_sync.create_record_idempotently', return_value='lead-1')
    def test_sync_uses_idempotent_path_when_module_has_external_key(self, create, has_field, find, add_tags):
        app = self.make_app()
        record_id = sync_application_to_zoho(app)
        self.assertEqual(record_id, 'lead-1')
        add_tags.assert_called_once_with('Leads', 'lead-1', ['Partner Application'])
        sent = create.call_args.kwargs['record']
        self.assertEqual(sent['External_Order_Key'], f'dcmn:application:{app.id}')
        self.assertEqual(create.call_args.kwargs['module_name'], 'Leads')
        app.refresh_from_db()
        self.assertTrue(app.zoho_synced)
        self.assertEqual(app.zoho_lead_id, 'lead-1')
        job = ZohoSyncJob.objects.get(order_type='application', order_id=app.id)
        self.assertEqual(job.zoho_record_id, 'lead-1')
        self.assertEqual(app.zoho_error, '')

    @patch('orders.zoho_sync.add_tags_to_record', return_value=True)
    @patch('orders.zoho_sync._find_leads_by_email', return_value=[])
    @patch('orders.zoho_sync.module_has_field', return_value=False)
    def test_sync_fallback_without_external_key_is_guarded_by_lead_id(self, has_field, find, add_tags):
        class FakeResponse:
            status_code = 201
            text = ''

            def json(self):
                return {'data': [{'code': 'SUCCESS', 'status': 'success', 'details': {'id': 'lead-2'}}]}

        app = self.make_app()
        with patch('orders.zoho_sync._request', return_value=FakeResponse()) as request:
            self.assertEqual(sync_application_to_zoho(app), 'lead-2')
            self.assertNotIn('External_Order_Key', request.call_args.kwargs['json']['data'][0])
        app.refresh_from_db()
        self.assertEqual(app.zoho_lead_id, 'lead-2')
        self.assertTrue(app.zoho_synced)

        with patch('orders.zoho_sync._request') as request:
            self.assertEqual(sync_application_to_zoho(app), 'lead-2')
            request.assert_not_called()

    @patch('orders.zoho_sync.add_tags_to_record', return_value=True)
    @patch('orders.zoho_sync._find_leads_by_email', return_value=[])
    @patch('orders.zoho_sync.module_has_field', return_value=True)
    @patch('orders.zoho_sync.create_record_idempotently', return_value='lead-3')
    def test_celery_task_marks_job_synced(self, create, has_field, find, add_tags):
        from .tasks import sync_order_to_zoho_task
        app = self.make_app()
        sync_order_to_zoho_task.apply(args=(app.id, 'application'))
        job = ZohoSyncJob.objects.get(order_type='application', order_id=app.id)
        self.assertEqual(job.status, ZohoSyncJob.STATUS_SYNCED)
        self.assertEqual(job.zoho_module, 'Leads')
        self.assertEqual(job.zoho_record_id, 'lead-3')
