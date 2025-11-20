# orders/tests.py
from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from rest_framework.test import APIClient
from .models import Track
from .constants import STAGE_DEFS


class TrackingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.token = 'testtoken'
        # monkeypatch token for tests
        settings.ZOHO_WEBHOOK_TOKEN = self.token

    def test_crm_create_and_public_get(self):
        url = reverse('tracking_crm_create')
        payload = {
            'name': 'John Doe',
            'email': 'john@example.com',
            'service': 'translation',
            'current_stage': 'document_received',
        }
        resp = self.client.post(url, payload, format='json', HTTP_X_ZOHO_TOKEN=self.token)
        self.assertEqual(resp.status_code, 201)
        tid = resp.data['tid']
        self.assertTrue(Track.objects.filter(tid=tid).exists())

        pub = reverse('tracking_public', kwargs={'tid': tid})
        r2 = self.client.get(pub)
        self.assertEqual(r2.status_code, 200)
        self.assertIn('steps', r2.data)

    def test_crm_update_with_mapping(self):
        # prepare track
        Track.objects.create(
            tid='ABC123', name='Jane Doe', email='jane@example.com',
            service='fbi_apostille', current_stage='document_received')

        url = reverse('tracking_crm_update')
        payload = {
            'tid': 'ABC123',
            'crm_stage_name': 'Submitted'
        }
        resp = self.client.post(url, payload, format='json', HTTP_X_ZOHO_TOKEN=self.token)
        self.assertEqual(resp.status_code, 200)
        t = Track.objects.get(tid='ABC123')
        self.assertEqual(t.current_stage, 'submitted')
