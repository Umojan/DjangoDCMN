"""Staff email in the legacy daily thread and the client confirmation (DESIGN §5.6, WP4)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.core import mail
from django.test import override_settings

from orders.services.notifications import send_staff_notification
from quotes import emails
from quotes.models import QuoteSubmission
from quotes.render import receipt_text
from quotes.tasks import send_quote_confirmation_email, send_quote_staff_email
from quotes.tests.test_contract import make_body
from quotes.tests.test_pipeline import PipelineTestCase, v5_quote

CLIENT_FROM = 'DC Mobile Notary <hello@example.com>'
STAFF_SUBJECT = '❓ New Quote Request'
confirmation_on = override_settings(QUOTE_V3_CONFIRMATION_EMAIL=True, EMAIL_CLIENT_FROM=CLIENT_FROM)


def staff_messages() -> list:
    return [message for message in mail.outbox if message.subject.startswith(STAFF_SUBJECT)]


def client_messages(quote: QuoteSubmission) -> list:
    return [message for message in mail.outbox if message.to == [quote.email] and quote.reference in message.subject]


class StaffTests(PipelineTestCase):
    def test_subject_and_thread_headers_equal_legacy_format(self):
        legacy_order = SimpleNamespace(id=41, name='Legacy Client', email='legacy@example.com', phone='202', address='')
        self.assertTrue(send_staff_notification(legacy_order, 'quote'))
        quote = v5_quote()

        emails.send_staff_email(quote)

        legacy, ours = mail.outbox
        self.assertEqual(ours.subject, legacy.subject)
        self.assertRegex(ours.subject, r'^❓ New Quote Request — \d{4}-\d{2}-\d{2}$')
        for header in ('In-Reply-To', 'References'):
            self.assertEqual(ours.extra_headers[header], legacy.extra_headers[header])
        self.assertRegex(ours.extra_headers['References'],
                         r'^<quote-request-thread-\d{4}-\d{2}-\d{2}@dcmobilenotary\.com>$')
        self.assertEqual(ours.from_email, legacy.from_email)
        self.assertEqual(ours.to, legacy.to)                 # QUOTES_NOTIFY_EMAILS empty → EMAIL_OFFICE_RECEIVER
        self.assertNotEqual(ours.extra_headers['Message-ID'], legacy.extra_headers['Message-ID'])

    def test_message_id_and_reply_to(self):
        quote = v5_quote(zoho_record_id='6686316000044400001')

        emails.send_staff_email(quote)

        (message,) = staff_messages()
        self.assertEqual(message.extra_headers['Message-ID'], f'<quote-{quote.reference}@dcmobilenotary.com>')
        self.assertEqual(message.reply_to, ['maria@example.com'])
        lines = message.body.splitlines()
        self.assertEqual(lines[0],
                         f'{quote.reference} · Apostille + Translation · Maria Fernández (new multi-service form)')
        self.assertIn(receipt_text(quote), message.body)
        self.assertIn(f'Zoho quote: {quote.zoho_url}', lines)
        self.assertIn(f'Admin: {quote.admin_url}', lines)
        html = message.alternatives[0][0]
        self.assertIn(f'href="{quote.zoho_url}"', html)
        quote.refresh_from_db()
        self.assertEqual(quote.staff_email_status, 'sent')
        self.assertIsNotNone(quote.staff_email_sent_at)

        emails.send_staff_email(quote)                     # a duplicate delivery of the task: sent once
        self.assertEqual(len(staff_messages()), 1)

    def test_zz_test_routed_to_test_recipients(self):
        real = v5_quote()
        test = v5_quote(make_body(contact={'full_name': 'ZZ TEST Maria'}))
        self.assertTrue(test.is_test)

        with override_settings(QUOTES_NOTIFY_EMAILS=['office2@example.com'],
                               QUOTES_TEST_NOTIFY_EMAILS=['qa@example.com']):
            emails.send_staff_email(real)
            emails.send_staff_email(test)

        self.assertEqual([m.to for m in staff_messages()], [['office2@example.com'], ['qa@example.com']])

    def test_held_sends_no_office_email(self):
        with override_settings(QUOTES_HOURLY_CEILING=0):
            quote = v5_quote()
        self.assertEqual((quote.sync_status, quote.staff_email_status), ('held', 'skipped'))
        QuoteSubmission.objects.filter(pk=quote.pk).update(staff_email_status='pending')
        quote.refresh_from_db()

        self.assertEqual(send_quote_staff_email.apply((quote.pk,)).get(), 'skipped')

        self.assertEqual(staff_messages(), [])
        self.assertTrue(any('held' in m.subject for m in mail.outbox))   # ops heard about the hold instead

    def test_suspect_first_line_says_nothing_created(self):
        quote = v5_quote(spam_status='suspect', spam_reasons=['turnstile_failed', 'message_urls'])

        emails.send_staff_email(quote)

        (message,) = staff_messages()
        first, second = message.body.splitlines()[:2]
        self.assertEqual(first, 'Possible spam (Turnstile failed, URLs in message) — nothing was created '
                                'automatically. If the request is real, use Create … or Add service…')
        self.assertTrue(second.startswith(quote.reference))
        self.assertIn('Zoho: syncing — see admin', message.body)

    def test_staff_email_task_runs_beat_check(self):
        quote = v5_quote()

        with patch('quotes.tasks.emails.check_beat') as check_beat:
            self.assertEqual(send_quote_staff_email.apply((quote.pk,)).get(), 'sent')
            self.assertEqual(send_quote_staff_email.apply((999999,)).get(), 'missing')

        self.assertEqual(check_beat.call_count, 2)
        self.assertEqual(len(staff_messages()), 1)

    def test_send_failures_retry_then_fail_and_alert(self):
        quote = v5_quote()

        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            for expected in ('pending', 'pending', 'failed'):
                emails.send_staff_email(quote)
                quote.refresh_from_db()
                self.assertEqual(quote.staff_email_status, expected)

        (alert,) = [m for m in mail.outbox if 'email not sent' in m.subject]
        self.assertNotIn(quote.email, alert.body)


class ConfirmationTests(PipelineTestCase):
    def test_off_by_default(self):
        quote = v5_quote()
        self.assertEqual(quote.confirmation_email_status, 'off')

        self.assertEqual(send_quote_confirmation_email.apply((quote.pk,)).get(), 'off')

        self.assertEqual(client_messages(quote), [])

    @confirmation_on
    def test_en_and_es_templates(self):
        quote = v5_quote()
        self.assertEqual(quote.confirmation_email_status, 'pending')

        emails.send_confirmation(quote)

        (en,) = client_messages(quote)
        self.assertEqual(en.subject, f'Quote Request {quote.reference} Received — DC Mobile Notary')
        self.assertEqual((en.from_email, en.reply_to), (CLIENT_FROM, ['support@dcmobilenotary.com']))
        self.assertEqual(en.extra_headers['Message-ID'], f'<quote-{quote.reference}-confirm@dcmobilenotary.com>')
        self.assertIn('- Apostille: Apostille · Federal · from $125 · + $25 per document\n'
                      '- Translation: Price after free review', en.body)
        self.assertIn('reply to this email or call +1 202-247-0837', en.body.lower())
        self.assertIn(quote.reference, en.alternatives[0][0])
        for promise in ('within', 'business day', 'hours'):
            self.assertNotIn(promise, en.body.lower())

        spanish = v5_quote(make_body(locale='es'))
        emails.send_confirmation(spanish)

        es = client_messages(spanish)[-1]
        self.assertEqual(es.subject, f'Solicitud {spanish.reference} recibida — DC Mobile Notary')
        self.assertIn('- Servicios de apostilla: Servicios de apostilla · Federal · desde $125 · + $25 por documento\n'
                      '- Servicios de traducción: Precio tras una revisión gratuita', es.body)
        self.assertIn('Precio tras una revisión gratuita', es.body)
        self.assertIn('Responda a este correo o llame al +1 202-247-0837', es.body)
        self.assertIn('lang="es"', es.alternatives[0][0])

    @confirmation_on
    def test_never_for_held_spam_test(self):
        held = v5_quote(sync_status='held', spam_status='held', confirmation_email_status='pending')
        spam = v5_quote(sync_status='spam', spam_status='spam', confirmation_email_status='pending')
        test = v5_quote(make_body(contact={'full_name': 'ZZ TEST Maria'}), confirmation_email_status='pending')

        for quote in (held, spam, test):
            emails.send_confirmation(quote)
            quote.refresh_from_db()
            with self.subTest(quote=quote.full_name, status=quote.sync_status):
                self.assertEqual(quote.confirmation_email_status, 'skipped')
        self.assertEqual([m for m in mail.outbox if m.subject.startswith('Quote Request')], [])

        QuoteSubmission.objects.filter(pk=test.pk).update(confirmation_email_status='pending')
        test.refresh_from_db()
        with override_settings(QUOTE_V3_TEST_EMAILS=True):
            emails.send_confirmation(test)
        self.assertEqual(len(client_messages(test)), 1)

    @confirmation_on
    def test_at_most_once_claim(self):
        quote = v5_quote()
        twin = QuoteSubmission.objects.get(pk=quote.pk)     # the same row loaded by a second worker

        emails.send_confirmation(quote)
        emails.send_confirmation(twin)
        send_quote_confirmation_email.apply((quote.pk,))

        self.assertEqual(len(client_messages(quote)), 1)
        quote.refresh_from_db()
        self.assertEqual(quote.confirmation_email_status, 'sent')

        in_flight = v5_quote()
        QuoteSubmission.objects.filter(pk=in_flight.pk).update(confirmation_email_status='sending')
        emails.send_confirmation(in_flight)
        self.assertEqual(client_messages(in_flight), [])
