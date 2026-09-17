"""Gated review flow tests.

    DJANGO_SETTINGS_MODULE=django_dcmn.settings_test python3 manage.py test reviews
"""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Review, ReviewRequest
from .services import make_token, parse_token, star_links
from .tasks import _send_review_request_email, send_review_reminders


@override_settings(
    BASE_URL='https://api.example.com',
    FRONTEND_URL='https://www.example.com',
    GOOGLE_REVIEW_URL='https://google.example/review',
    TRUSTPILOT_TRIGGER_EMAIL='afs@trustpilot.example',
    REVIEWS_NOTIFY_EMAILS=['manager@example.com'],
    REVIEWS_POSITIVE_THRESHOLD=4,
)
class ReviewGatingTests(TestCase):
    def setUp(self):
        self.rr = ReviewRequest.objects.create(
            email='client@example.com', name='John Doe', phone='+12025550100',
            zoho_contact_id='1', zoho_deal_id='999', zoho_module='FBI', tracking_id='ABC123',
            review_type='google', is_sent=True, sent_at=timezone.now(),
        )
        self.token = make_token(self.rr)
        zoho = mock.patch('reviews.services.push_negative_to_zoho')
        self.zoho_mock = zoho.start()
        self.addCleanup(zoho.stop)

    # --- tokens -----------------------------------------------------------
    def test_token_roundtrip_and_tamper(self):
        self.assertEqual(parse_token(self.token).id, self.rr.id)
        self.assertIsNone(parse_token(self.token + 'x'))
        self.assertIsNone(parse_token('garbage'))

    def test_star_links_point_to_api(self):
        links = star_links(self.rr)
        self.assertEqual([l['stars'] for l in links], [1, 2, 3, 4, 5])
        self.assertTrue(links[0]['url'].startswith('https://api.example.com/api/reviews/r/'))
        self.assertTrue(links[4]['url'].endswith('/5/'))

    # --- email ------------------------------------------------------------
    def test_review_email_contains_five_star_links_and_no_trustpilot_bcc(self):
        self.rr.review_type = 'trustpilot'
        self.rr.save()
        _send_review_request_email(self.rr)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.bcc, [])
        for n in range(1, 6):
            self.assertIn(f'/api/reviews/r/{self.token}/{n}/', msg.body)
        self.rr.refresh_from_db()
        self.assertTrue(self.rr.stars_email_sent)

    # --- rate: GET is an interstitial, never records ------------------------
    def test_get_star_link_does_not_record_rating(self):
        resp = self.client.get(f'/api/reviews/r/{self.token}/5/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'method="post"')
        self.assertFalse(Review.objects.exists())

    def test_get_invalid_token_404(self):
        resp = self.client.get('/api/reviews/r/bad-token/5/')
        self.assertEqual(resp.status_code, 404)

    # --- positive → Google -------------------------------------------------
    def test_positive_first_time_customer_goes_to_google(self):
        resp = self.client.post(f'/api/reviews/r/{self.token}/5/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'https://google.example/review')
        review = Review.objects.get(request=self.rr)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.route, Review.ROUTE_GOOGLE)
        self.assertEqual(len(mail.outbox), 0)  # no manager alert, no trustpilot
        self.zoho_mock.assert_not_called()

    def test_threshold_is_inclusive(self):
        resp = self.client.post(f'/api/reviews/r/{self.token}/4/')
        self.assertEqual(resp['Location'], 'https://google.example/review')

    # --- positive returning → Trustpilot ---------------------------------------
    def test_positive_returning_customer_gets_trustpilot_invite(self):
        self.rr.review_type = 'trustpilot'
        self.rr.save()
        resp = self.client.post(f'/api/reviews/r/{self.token}/5/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], f'https://www.example.com/review-thanks?t={self.token}')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['client@example.com'])
        self.assertEqual(mail.outbox[0].bcc, ['afs@trustpilot.example'])
        self.rr.refresh_from_db()
        self.assertIsNotNone(self.rr.trustpilot_invite_sent_at)
        # second click must not send a second invite
        self.client.post(f'/api/reviews/r/{self.token}/5/')
        self.assertEqual(len(mail.outbox), 1)

    # --- negative → internal -----------------------------------------------------
    def test_negative_goes_to_feedback_page_and_alerts_managers(self):
        resp = self.client.post(f'/api/reviews/r/{self.token}/2/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], f'https://www.example.com/feedback?t={self.token}')
        review = Review.objects.get(request=self.rr)
        self.assertEqual(review.route, Review.ROUTE_INTERNAL)
        # eager celery: the delayed alert ran immediately
        self.assertEqual(len(mail.outbox), 1)
        alert = mail.outbox[0]
        self.assertEqual(alert.to, ['manager@example.com'])
        self.assertIn('Negative feedback: 2★', alert.subject)
        self.assertIn('ABC123', alert.subject)
        self.assertEqual(alert.reply_to, ['client@example.com'])
        self.assertNotIn('google.example', alert.body)
        review.refresh_from_db()
        self.assertTrue(review.manager_notified)
        self.assertFalse(review.notified_with_feedback)
        self.zoho_mock.assert_called_once()

    def test_first_rating_wins(self):
        self.client.post(f'/api/reviews/r/{self.token}/2/')
        resp = self.client.post(f'/api/reviews/r/{self.token}/5/')
        self.assertEqual(resp['Location'], f'https://www.example.com/feedback?t={self.token}')
        self.assertEqual(Review.objects.get(request=self.rr).rating, 2)

    def test_feedback_submit_after_negative_rating_sends_update(self):
        self.client.post(f'/api/reviews/r/{self.token}/1/')
        mail.outbox.clear()
        self.zoho_mock.reset_mock()
        resp = self.client.post('/api/reviews/feedback/', {
            'token': self.token, 'message': 'Courier was 3 days late.', 'callback_requested': 'true', 'phone': '+1 202 555 0199',
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        review = Review.objects.get(request=self.rr)
        self.assertEqual(review.feedback_text, 'Courier was 3 days late.')
        self.assertTrue(review.callback_requested)
        self.assertEqual(review.callback_phone, '+1 202 555 0199')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Feedback added: 1★', mail.outbox[0].subject)
        self.assertIn('Courier was 3 days late.', mail.outbox[0].body)
        self.assertIn('Customer asked to be called back', mail.outbox[0].body)
        review.refresh_from_db()
        self.assertTrue(review.notified_with_feedback)
        self.zoho_mock.assert_called_once()
        # a third notify is a no-op
        resp = self.client.post('/api/reviews/feedback/', {'token': self.token, 'message': 'Courier was 3 days late.'},
                                content_type='application/json')
        self.assertEqual(len(mail.outbox), 1)

    def test_feedback_before_alert_is_sent_includes_text_once(self):
        # simulate: rating recorded but delayed alert not yet fired
        with mock.patch('reviews.views.notify_negative_task') as task:
            self.client.post(f'/api/reviews/r/{self.token}/2/')
            task.apply_async.assert_called_once()
        self.client.post('/api/reviews/feedback/', {'token': self.token, 'message': 'Wrong seal.'}, content_type='application/json')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Negative feedback: 2★', mail.outbox[0].subject)
        self.assertIn('Wrong seal.', mail.outbox[0].body)
        review = Review.objects.get(request=self.rr)
        self.assertTrue(review.manager_notified and review.notified_with_feedback)

    def test_feedback_without_rating_creates_internal_review(self):
        resp = self.client.post('/api/reviews/feedback/', {'token': self.token, 'message': 'Hello', 'rating': 3},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        review = Review.objects.get(request=self.rr)
        self.assertEqual(review.route, Review.ROUTE_INTERNAL)
        self.assertEqual(review.rating, 3)
        self.assertEqual(len(mail.outbox), 1)

    def test_feedback_validation(self):
        resp = self.client.post('/api/reviews/feedback/', {'token': self.token}, content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('detail', resp.json())
        resp = self.client.post('/api/reviews/feedback/', {'token': 'nope', 'message': 'x'}, content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    # --- context endpoint -------------------------------------------------------
    def test_context_endpoint(self):
        self.client.post(f'/api/reviews/r/{self.token}/2/')
        resp = self.client.get(f'/api/reviews/r/{self.token}/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['name'], 'John')
        self.assertEqual(data['tracking_id'], 'ABC123')
        self.assertEqual(data['service_label'], 'FBI Apostille')
        self.assertEqual(data['rating'], 2)
        self.assertEqual(data['route'], 'internal')
        self.assertFalse(data['feedback_submitted'])
        self.assertEqual(self.client.get('/api/reviews/r/bad/').status_code, 404)

    # --- reminder ------------------------------------------------------------------
    def test_reminder_only_for_star_emails_without_rating(self):
        now = timezone.now()
        self.rr.stars_email_sent = True
        self.rr.sent_at = now - timedelta(days=4)
        self.rr.save()
        rated = ReviewRequest.objects.create(email='rated@example.com', zoho_contact_id='2', zoho_module='FBI',
                                             is_sent=True, stars_email_sent=True, sent_at=now - timedelta(days=4))
        Review.objects.create(request=rated, rating=5, route='google', rated_at=now)
        ReviewRequest.objects.create(email='old-flow@example.com', zoho_contact_id='3', zoho_module='FBI',
                                     is_sent=True, stars_email_sent=False, sent_at=now - timedelta(days=4))
        ReviewRequest.objects.create(email='fresh@example.com', zoho_contact_id='4', zoho_module='FBI',
                                     is_sent=True, stars_email_sent=True, sent_at=now - timedelta(days=1))
        ReviewRequest.objects.create(email='ancient@example.com', zoho_contact_id='5', zoho_module='FBI',
                                     is_sent=True, stars_email_sent=True, sent_at=now - timedelta(days=40))
        self.assertEqual(send_review_reminders(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['client@example.com'])
        self.assertIn('Quick question about your order', mail.outbox[0].subject)
        self.assertIn(f'/api/reviews/r/{self.token}/5/', mail.outbox[0].body)
        self.rr.refresh_from_db()
        self.assertIsNotNone(self.rr.reminder_sent_at)
        # second run: nothing
        self.assertEqual(send_review_reminders(), 0)
