"""crm.alerts: hourly dedupe, no PII in alert mails, beat heartbeat check (WP1)."""

import re
import time
import warnings
from unittest.mock import patch

from django.core import mail
from django.core.cache import CacheKeyWarning, cache
from django.test import override_settings

from crm import alerts
from crm.testing import NoNetworkTestCase

# Independent of crm.alerts' own patterns: any address, or a 10-digit number not inside a longer digit run.
EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
PHONE = re.compile(r'(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)')

ZOHO_ID = '5725767000012345678'
RECORD_URL = f'https://crm.zoho.com/crm/org882204370/tab/CustomModule7/{ZOHO_ID}'


@override_settings(OPS_ALERT_EMAILS=['ops@example.com'])
class AlertTests(NoNetworkTestCase):
    def test_dedupes_within_an_hour(self):
        sent_at = time.time()

        self.assertTrue(alerts.alert_ops('zoho_auth', 'token', 'Zoho token refresh failing', 'Check Z1.'))
        self.assertFalse(alerts.alert_ops('zoho_auth', 'token', 'Zoho token refresh failing', 'Check Z1.'))
        with patch('time.time', return_value=sent_at + alerts.ALERT_DEDUPE_S - 60):
            self.assertFalse(alerts.alert_ops('zoho_auth', 'token', 'again', 'again'))
        self.assertTrue(alerts.alert_ops('zoho_auth', 'other-key', 'Another key', 'Sent.'))
        self.assertEqual(len(mail.outbox), 2)

        with patch('time.time', return_value=sent_at + alerts.ALERT_DEDUPE_S + 60):
            self.assertTrue(alerts.alert_ops('zoho_auth', 'token', 'Zoho token refresh failing', 'Check Z1.'))
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(mail.outbox[0].to, ['ops@example.com'])
        self.assertTrue(mail.outbox[0].subject.startswith(alerts.SUBJECT_PREFIX))

    def test_key_with_spaces_is_a_valid_cache_key(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', CacheKeyWarning)
            self.assertTrue(alerts.alert_ops('quotes_held', '2026-11-27 14', 'Held', 'Held.'))
            self.assertFalse(alerts.alert_ops('quotes_held', '2026-11-27 14', 'Held', 'Held.'))

    def test_body_has_no_email_or_phone(self):
        body = (
            f'Link failed for Q-482913 (jane.doe@example.com, +1 (202) 555-0123).\n'
            f'Also 202-555-0123, (202) 555 0123, 202.555.0123 and 2025550123.\n'
            f'Record {ZOHO_ID}: {RECORD_URL}\n'
            'Admin: https://api.dcmobilenotary.com/admin/quotes/quotesubmission/57/change/'
        )

        self.assertTrue(alerts.alert_ops('link_failed', 'qr:57', 'Q-482913 for jane.doe@example.com', body))

        message = mail.outbox[0]
        for text in (message.subject, message.body):
            self.assertIsNone(EMAIL.search(text), text)
            self.assertIsNone(PHONE.search(text), text)
        self.assertIn('Q-482913', message.subject)
        self.assertIn(RECORD_URL, message.body)
        self.assertIn(f'Record {ZOHO_ID}', message.body)
        self.assertIn('/admin/quotes/quotesubmission/57/change/', message.body)

    @override_settings(OPS_ALERT_EMAILS=[], EMAIL_OFFICE_RECEIVER=['office@example.com'])
    def test_falls_back_to_office_receiver(self):
        self.assertTrue(alerts.alert_ops('zoho_auth', 'token', 'Subject', 'Body'))
        self.assertEqual(mail.outbox[0].to, ['office@example.com'])

    @override_settings(OPS_ALERT_EMAILS=[], EMAIL_OFFICE_RECEIVER=[])
    def test_no_recipient_sends_nothing(self):
        with self.assertLogs('crm.alerts', level='WARNING'):
            self.assertFalse(alerts.alert_ops('zoho_auth', 'token', 'Subject', 'Body'))
        self.assertEqual(mail.outbox, [])

    def test_failed_send_is_retried_by_the_next_alert(self):
        with patch('django.core.mail.EmailMessage.send', side_effect=OSError('smtp down')), \
                self.assertLogs('crm.alerts', level='ERROR'):
            self.assertFalse(alerts.alert_ops('zoho_auth', 'token', 'Subject', 'Body'))

        self.assertTrue(alerts.alert_ops('zoho_auth', 'token', 'Subject', 'Body'))
        self.assertEqual(len(mail.outbox), 1)


@override_settings(OPS_ALERT_EMAILS=['ops@example.com'])
class CheckBeatTests(NoNetworkTestCase):
    def test_fresh_heartbeat_sends_nothing(self):
        cache.set(alerts.BEAT_HEARTBEAT_KEY, time.time() - 120)

        self.assertFalse(alerts.check_beat())
        self.assertEqual(mail.outbox, [])

    def test_missing_heartbeat_alerts_only_after_the_grace_period(self):
        # Regression (review 2026-11-27, legacy-rollout-2): the first check after a fresh deploy or a Redis
        # restart finds no heartbeat yet; that is not a stopped beat.
        self.assertFalse(alerts.check_beat())
        self.assertEqual(mail.outbox, [])

        cache.delete(alerts.BEAT_CHECK_KEY)
        with patch('crm.alerts.time.time', return_value=time.time() + alerts.BEAT_STALE_AFTER_S + 60):
            self.assertTrue(alerts.check_beat())
        (alert,) = mail.outbox
        self.assertIn('no heartbeat recorded', alert.body)

        cache.set(alerts.BEAT_HEARTBEAT_KEY, time.time())   # beat is back: the grace period starts again next time
        cache.delete(alerts.BEAT_CHECK_KEY)
        self.assertFalse(alerts.check_beat())
        self.assertIsNone(cache.get(alerts.BEAT_FIRST_CHECK_KEY))

    def test_missing_or_stale_heartbeat_alerts_once(self):
        cache.set(alerts.BEAT_HEARTBEAT_KEY, time.time() - alerts.BEAT_STALE_AFTER_S - 30)
        self.assertTrue(alerts.check_beat())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('reconciler', mail.outbox[0].body.lower())

        cache.delete(alerts.BEAT_CHECK_KEY)
        cache.set(alerts.BEAT_HEARTBEAT_KEY, time.time() - alerts.BEAT_STALE_AFTER_S - 60)
        self.assertFalse(alerts.check_beat())  # same alert within the hour
        self.assertEqual(len(mail.outbox), 1)

    def test_checks_at_most_every_five_minutes(self):
        cache.set(alerts.BEAT_HEARTBEAT_KEY, time.time())
        self.assertFalse(alerts.check_beat())

        cache.delete(alerts.BEAT_HEARTBEAT_KEY)
        self.assertFalse(alerts.check_beat())  # not checked again yet
        self.assertEqual(mail.outbox, [])
