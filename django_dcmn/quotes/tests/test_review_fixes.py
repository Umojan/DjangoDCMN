"""Regression tests of the review of 2026-11-27 (IMPLEMENTATION-STATUS "Review 2026-11-27"); one per finding id."""

import copy
import uuid
from unittest.mock import patch

import requests
from anymail.exceptions import AnymailRequestsAPIError
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from crm.models import ServiceLink
from orders.models import Track
from quotes import emails, flags, pipeline
from quotes.apps import pii_hash_salt_check
from quotes.models import QuoteSubmission
from quotes.tasks.maintenance import erase_quote_pii
from quotes.tests.test_contract import load_fixture, make_body
from quotes.tests.test_emails import CLIENT_FROM, client_messages, confirmation_on
from quotes.tests.test_pipeline import PipelineTestCase, v5_quote

URL_NAME = 'Refund ready http://evil.example/claim'


def anymail_error(cause: Exception) -> Exception:
    """What anymail raises for a requests failure: an error that is also the requests exception's type."""
    error_class = type('AnymailRequestsAPIError', (AnymailRequestsAPIError, type(cause)), {})
    try:
        raise error_class('Error posting to https://api.resend.com/emails:') from cause
    except Exception as exc:   # with __cause__ set, as anymail raises it
        return exc


@confirmation_on
class UnknownSendOutcomeTests(PipelineTestCase):
    """correctness-6: a send that may have reached the provider is never retried into a second email."""

    def pending_quote(self) -> QuoteSubmission:
        quote = v5_quote()
        self.assertEqual(quote.confirmation_email_status, 'pending')
        return quote

    def test_read_timeout_is_failed_and_alerted_not_retried(self):
        quote = self.pending_quote()

        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=anymail_error(requests.ReadTimeout())), \
                self.assertLogs('quotes.emails', 'WARNING'), override_settings(OPS_ALERT_EMAILS=['ops@example.com']):
            emails.send_confirmation(quote)

        quote.refresh_from_db()
        self.assertEqual(quote.confirmation_email_status, 'failed')   # the reconciler republishes only pending
        (alert,) = [m for m in mail.outbox if m.to == ['ops@example.com']]
        self.assertIn('may have been sent', alert.subject)

    def test_connect_timeout_and_http_rejection_are_retried(self):
        for error in (anymail_error(requests.ConnectTimeout()), AnymailRequestsAPIError('HTTP 422')):
            with self.subTest(error=type(error).__mro__[1].__name__):
                quote = self.pending_quote()
                with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=error), \
                        self.assertLogs('quotes.emails', 'WARNING'):
                    emails.send_confirmation(quote)
                quote.refresh_from_db()
                self.assertEqual(quote.confirmation_email_status, 'pending')


class AttributionLeadTypeTests(PipelineTestCase):
    """zoho-contract-2: a v3 attribution row carries the picklist value 'Form Submission', never 'Form'."""

    def test_attribution_lead_type_is_form_submission(self):
        quote = v5_quote()
        self.assertNotIn('lead_type', quote.attribution_data)

        pipeline.step_attribution(quote)

        (post,) = self.calls('POST', 'Lead_Attribution_Records')
        self.assertEqual(post.json['data'][0]['Lead_Type'], 'Form Submission')


@override_settings(QUOTE_V3_ENABLED=True, QUOTE_V3_TEST_ONLY=False, QUOTE_V3_CONFIRMATION_EMAIL=True,
                   EMAIL_CLIENT_FROM=CLIENT_FROM)
class ConfirmationRelayTests(PipelineTestCase):
    """security-confirmation-email-attacker-text: no branded email to a typed-in address with a link in the name."""

    def test_url_in_name_gets_no_confirmation(self):
        body = make_body(contact={'email': 'victim@example.com', 'full_name': URL_NAME})

        response = self.client.post(reverse('quote_requests_submit'), body, content_type='application/json')

        self.assertEqual(response.status_code, 201, response.content[:300])
        self.assertEqual(response.json()['confirmation_email'], 'off')
        quote = QuoteSubmission.objects.get(reference=response.json()['reference'])
        self.assertIn('url_in_name', quote.spam_reasons)
        self.assertEqual(quote.confirmation_email_status, 'off')
        self.assertFalse(emails.confirmation_allowed(quote))
        QuoteSubmission.objects.filter(pk=quote.pk).update(confirmation_email_status='pending')
        emails.send_confirmation(quote)
        self.assertEqual(client_messages(quote), [])

    def test_suspect_quote_gets_no_confirmation_and_greeting_never_has_a_link(self):
        quote = v5_quote(spam_status='suspect', spam_reasons=['disposable_email', 'no_timing'])
        self.assertFalse(emails.confirmation_allowed(quote))

        QuoteSubmission.objects.filter(pk=quote.pk).update(full_name=URL_NAME)
        quote.refresh_from_db()
        self.assertEqual(emails.confirmation_context(quote)['name'], '')


class ErasePiiTests(PipelineTestCase):
    """security-erase-pii-incomplete: free-text answers, summaries, page URL, attribution and Track rows."""

    def test_erase_blanks_free_text_page_url_attribution_and_tracks(self):
        body = copy.deepcopy(load_fixture('svc_retrieval')['body'])
        body['submission_id'] = str(uuid.uuid4())
        body['services'][0]['answers']['record'] = 'Birth certificate of Maria Fernandez, born 03/04/1985'
        body['page']['url'] = 'https://www.dcmobilenotary.com/get-a-quote?email=maria@x.com'
        quote = v5_quote(body)
        line = quote.lines.get()
        self.assertIn('Maria Fernandez', line.summary_server)
        link = ServiceLink.objects.create(source_kind='quote', source_key=f'qr:{quote.pk}', quote=quote,
                                          service_key='apostille', external_key=f'dcmn:qr:{quote.pk}:apostille',
                                          tracking_id='TRK-ERASE1')
        Track.objects.create(tid=link.tracking_id, service='state_apostille', data={
            'name': 'Maria Fernández', 'email': 'maria@example.com', 'service': 'state_apostille',
            'origin': 'crm', 'source_key': f'qr:{quote.pk}', 'current_stage': 'document_received'})

        erase_quote_pii(quote)

        quote.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((quote.page_url, quote.attribution_data), ('', None))
        self.assertEqual(line.answers['record'], '')
        self.assertNotIn('Maria', line.summary_server)
        self.assertEqual(line.summary_client, '')
        self.assertEqual(line.service_key, 'retrieval')
        data = Track.objects.get(tid=link.tracking_id).data
        self.assertEqual((data['name'], data['email'], data['service']), ('', '', 'state_apostille'))


class PiiHashSaltTests(PipelineTestCase):
    """security-pii-hash-salt-unenforced: v3 refuses to run (and check --deploy fails) without a salt."""

    @override_settings(QUOTE_V3_ENABLED=True, PII_HASH_SALT='', OPS_ALERT_EMAILS=['ops@example.com'])
    def test_v3_refused_without_salt(self):
        with self.assertLogs('quotes.flags', 'ERROR'):
            self.assertFalse(flags.quote_v3_enabled())
            response = self.client.get(reverse('quote_requests_config'))
        self.assertEqual(response.status_code, 503)
        (error,) = pii_hash_salt_check()
        self.assertEqual(error.id, 'quotes.E001')
        self.assertTrue(any('PII_HASH_SALT' in m.subject for m in mail.outbox))

        with override_settings(PII_HASH_SALT='x' * flags.PII_HASH_SALT_MIN):
            self.assertTrue(flags.quote_v3_enabled())
            self.assertEqual(pii_hash_salt_check(), [])
