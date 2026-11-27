"""Layered idempotency (DESIGN §3.6): same submission_id, amendments, soft dedupe, repeat flag."""

from datetime import timedelta
from unittest.mock import patch

from django.test import tag
from django.utils import timezone

from crm.testing import NoNetworkTestCase
from quotes import intake
from quotes.intake import RequestMeta, save_submission
from quotes.models import QuoteSubmission
from quotes.render import comments_text
from quotes.tests.test_contract import enabled, make_body, normalised, post


def changed(body: dict, *, phone: str | None = None, country: str | None = None) -> dict:
    """The same submission re-sent with another phone and/or apostille country."""
    services = [dict(line, answers=dict(line['answers'])) for line in body['services']]
    if country:
        services[0]['answers']['country'] = country
    contact = {**body['contact'], **({'phone': phone} if phone else {})}
    return {**body, 'contact': contact, 'services': services}


@enabled
class SameIdTests(NoNetworkTestCase):
    def test_returns_original_reference_200(self):
        body = make_body()
        first = post(self.client, body)
        self.assertEqual(first.status_code, 201)

        again = post(self.client, body)

        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {
            'ok': True, 'duplicate': True, 'reference': first.json()['reference'],
            'confirmation_email': 'off', 'received_at': first.json()['received_at'],
        })
        quote = QuoteSubmission.objects.get()
        self.assertEqual(quote.amendments, [])
        self.assertEqual(quote.lines.count(), 2)

    def test_changed_content_appends_amendment(self):
        body = make_body()
        reference = post(self.client, body).json()['reference']

        again = post(self.client, changed(body, phone='+1 202 555 0199', country='MX'))

        self.assertEqual((again.status_code, again.json()['reference']), (200, reference))
        quote = QuoteSubmission.objects.get()
        self.assertEqual(len(quote.amendments), 1)
        amendment = quote.amendments[0]
        self.assertEqual(amendment['changed'], ['contact.phone', 'apostille.country'])
        self.assertEqual(amendment['payload']['contact']['phone'], '+1 202 555 0199')
        self.assertNotIn('turnstile_token', amendment['payload'])
        self.assertEqual(quote.phone_e164, '+12025550147')                       # the original stands
        self.assertEqual(quote.lines.get(service_key='apostille').answers['country'], 'ES')

        post(self.client, changed(body, phone='+1 202 555 0199', country='MX'))   # the same change again
        self.assertEqual(len(QuoteSubmission.objects.get().amendments), 1)

    def test_amendment_limit_5_per_24h(self):
        body = make_body()
        post(self.client, body)
        for n in range(7):
            response = post(self.client, changed(body, phone=f'+1 202 555 01{n:02d}'))
            self.assertEqual(response.status_code, 200)

        quote = QuoteSubmission.objects.get()
        self.assertEqual(len(quote.amendments), 5)
        self.assertEqual(quote.contract_warnings.count('amendment_limit'), 1)

        other = make_body(contact={'email': 'late@example.com'})
        post(self.client, other)
        QuoteSubmission.objects.filter(email_normalized='late@example.com').update(
            created_at=timezone.now() - timedelta(hours=25))
        post(self.client, changed(other, country='MX'))
        late = QuoteSubmission.objects.get(email_normalized='late@example.com')
        self.assertEqual((late.amendments, late.contract_warnings[-1]), ([], 'amendment_limit'))

    def test_invalid_body_on_duplicate_returns_original(self):
        body = make_body()
        reference = post(self.client, body).json()['reference']
        broken = {**body, 'contact': {**body['contact'], 'email': 'not-an-email'}, 'services': []}

        response = post(self.client, broken)

        self.assertEqual((response.status_code, response.json()['reference']), (200, reference))
        self.assertEqual(QuoteSubmission.objects.get().amendments, [])

    @tag('postgres')
    def test_integrity_error_race_returns_duplicate(self):
        """Another request inserted the same submission_id between the re-check and this insert."""
        nq = normalised(make_body())
        meta = RequestMeta(ip='10.0.0.1', user_agent='test', turnstile_result='skipped', attribution=None)
        winner = save_submission(nq, meta).quote
        self.assertTrue(QuoteSubmission.objects.filter(pk=winner.pk).exists())

        real_find = intake.find_by_submission_id
        with patch('quotes.intake.find_by_submission_id', side_effect=[None, real_find(nq.core['submission_id'])]):
            result = save_submission(nq, meta)

        self.assertEqual((result.created, result.duplicate, result.amended), (False, True, False))
        self.assertEqual(result.quote.pk, winner.pk)
        self.assertEqual(QuoteSubmission.objects.count(), 1)


@enabled
class SoftDedupeTests(NoNetworkTestCase):
    def test_same_email_same_hash_10min(self):
        body = make_body()
        first = post(self.client, body).json()['reference']

        lost_session = {**body, 'submission_id': make_body()['submission_id']}
        response = post(self.client, lost_session)
        self.assertEqual((response.status_code, response.json()['duplicate'], response.json()['reference']),
                         (200, True, first))
        self.assertEqual(QuoteSubmission.objects.count(), 1)

        other_case = {**lost_session, 'contact': {**body['contact'], 'email': 'MARIA@example.com'}}
        self.assertEqual(post(self.client, other_case).json()['reference'], first)   # email_normalized matches

        QuoteSubmission.objects.update(created_at=timezone.now() - timedelta(minutes=11))
        later = post(self.client, {**body, 'submission_id': make_body()['submission_id']})
        self.assertEqual(later.status_code, 201)
        self.assertNotEqual(later.json()['reference'], first)


@enabled
class RepeatTests(NoNetworkTestCase):
    def test_same_email_24h_sets_possible_repeat_of(self):
        first = post(self.client, make_body()).json()['reference']
        QuoteSubmission.objects.update(created_at=timezone.now() - timedelta(hours=3))

        second = post(self.client, make_body(contact={'message': 'Also two birth certificates.'}))

        self.assertEqual(second.status_code, 201)
        quote = QuoteSubmission.objects.get(reference=second.json()['reference'])
        self.assertEqual(quote.possible_repeat_of.reference, first)
        self.assertEqual(comments_text(quote).splitlines()[1], f'⚠ Possible repeat of {first} (same email, 3 h ago)')

        QuoteSubmission.objects.update(created_at=timezone.now() - timedelta(hours=25))
        third = post(self.client, make_body(contact={'message': 'A week later.'}))
        self.assertIsNone(QuoteSubmission.objects.get(reference=third.json()['reference']).possible_repeat_of)
