"""Anti-spam and replay handling, in the order of DESIGN §3.5."""

from datetime import timedelta
from unittest.mock import patch

import requests
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from django.utils import timezone

from crm.testing import FakeResponse, NoNetworkTestCase, make_quote_submission
from quotes import antispam
from quotes.models import QuoteRejection, QuoteSubmission
from quotes.tests.test_contract import enabled, load_fixture, make_body, next_ip, normalised, post, row_counts

SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
PASS = FakeResponse(200, {'success': True, 'error-codes': []})
FAIL = FakeResponse(200, {'success': False, 'error-codes': ['invalid-input-response']})
with_secret = override_settings(TURNSTILE_SECRET_KEY='test-secret')


def siteverify_calls(http) -> list:
    return [call for call in http.calls if call.url == SITEVERIFY]


@enabled
class HoneypotTests(NoNetworkTestCase):
    def test_fake_200_and_rejection_row_only(self):
        for field in ('website_confirm', 'fax'):
            with self.subTest(field=field):
                response = post(self.client, make_body(**{field: 'https://spam.example'}))

                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual({k: data[k] for k in ('ok', 'reference', 'confirmation_email')},
                                 {'ok': True, 'reference': None, 'confirmation_email': 'off'})
                rejection = QuoteRejection.objects.latest('id')
                self.assertEqual(rejection.reason, 'honeypot')
                self.assertEqual(len(rejection.email_hash), 64)
                self.assertNotIn('maria', rejection.email_hash)
        self.assertEqual(row_counts(), {'submissions': 0, 'lines': 0, 'rejections': 2})
        self.assertNoHttp()


@enabled
class TimeTrapTests(NoNetworkTestCase):
    def test_fake_200(self):
        body = load_fixture('time_trap')['body']
        response = post(self.client, body)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['reference'])
        self.assertEqual(QuoteRejection.objects.get().reason, 'time_trap')
        self.assertEqual(QuoteRejection.objects.get().elapsed_ms, 1200)
        self.assertFalse(QuoteSubmission.objects.exists())


@enabled
@with_secret
class TurnstileTests(NoNetworkTestCase):
    def test_failed_403_missing_error_skipped_allowed(self):
        self.http.queue(FAIL)
        response = post(self.client, make_body(turnstile_token='spent'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {
            'ok': False, 'detail': 'Verification failed, please reload the page and try again.',
            'errors': {'turnstile': ['failed']},
        })
        self.assertEqual(QuoteRejection.objects.get().reason, 'turnstile')

        cases = (
            ('missing', {'turnstile_token': None}, None),
            ('error', {'turnstile_token': 'tok-1'}, requests.ConnectionError('unreachable')),
            ('error', {'turnstile_token': 'tok-2'}, FakeResponse(503, None)),
            ('passed', {'turnstile_token': 'tok-3'}, PASS),
        )
        for number, (result, top, answer) in enumerate(cases):
            with self.subTest(result=result, number=number):
                if answer is not None:
                    self.http.queue(answer)
                response = post(self.client, make_body(contact={'email': f'case{number}@example.com'}, **top))
                self.assertEqual(response.status_code, 201)
                quote = QuoteSubmission.objects.get(reference=response.json()['reference'])
                self.assertEqual(quote.turnstile_result, result)

        with override_settings(TURNSTILE_SECRET_KEY=''):
            calls = len(self.http.calls)
            response = post(self.client, make_body(contact={'email': 'skip@example.com'}, turnstile_token='tok-4'))
            self.assertEqual(response.status_code, 201)
            self.assertEqual(QuoteSubmission.objects.get(reference=response.json()['reference']).turnstile_result,
                             'skipped')
            self.assertEqual(len(self.http.calls), calls)

    def test_missing_vs_error_distinguished(self):
        self.assertEqual(antispam.verify_turnstile(None, '10.0.0.1', 'sid'), 'missing')
        self.assertEqual(antispam.verify_turnstile('', '10.0.0.1', 'sid'), 'missing')
        self.assertNoHttp()

        self.http.queue(FakeResponse(500, None))
        self.assertEqual(antispam.verify_turnstile('tok', '10.0.0.1', 'sid'), 'error')
        self.http.queue(requests.Timeout('slow'))
        self.assertEqual(antispam.verify_turnstile('tok', '10.0.0.1', 'sid'), 'error')
        self.http.queue(FakeResponse(200, {'success': False, 'error-codes': ['invalid-input-secret']}))
        self.assertEqual(antispam.verify_turnstile('tok', '10.0.0.1', 'sid'), 'error')
        self.http.queue(FakeResponse(200, {'success': False, 'error-codes': ['timeout-or-duplicate']}))
        self.assertEqual(antispam.verify_turnstile('tok', '10.0.0.1', 'sid'), 'failed')

    @override_settings(QUOTE_TURNSTILE_REQUIRED=True)
    def test_turnstile_required_rejects_missing_token_locally(self):
        response = post(self.client, make_body(turnstile_token=None))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['errors'], {'turnstile': ['missing']})
        self.assertEqual(QuoteRejection.objects.get().reason, 'turnstile')
        self.assertFalse(QuoteSubmission.objects.exists())
        self.assertNoHttp()

    def test_timeouts_are_2_3(self):
        self.http.queue(PASS)
        self.assertEqual(antispam.verify_turnstile('tok', '10.0.0.9', 'sid'), 'passed')

        call = self.http.calls[0]
        self.assertEqual((call.method, call.url, call.timeout), ('POST', SITEVERIFY, (2, 3)))


@enabled
class ThrottleTests(NoNetworkTestCase):
    def test_ip_burst_429_retry_after(self):
        ip = next_ip()
        statuses = [post(self.client, make_body(contact={'email': f'try{n}@example.com'}), ip=ip).status_code
                    for n in range(5)]
        self.assertEqual(statuses, [201] * 5)

        response = post(self.client, make_body(contact={'email': 'sixth@example.com'}), ip=ip)
        self.assertEqual(response.status_code, 429)
        data = response.json()
        self.assertEqual((data['ok'], data['detail']), (False, 'throttled'))
        self.assertGreaterEqual(data['retry_after'], 1)
        self.assertEqual(response['Retry-After'], str(data['retry_after']))
        self.assertEqual(QuoteSubmission.objects.count(), 5)
        self.assertFalse(QuoteRejection.objects.exists())

    def test_email_throttle_429(self):
        for n in range(3):
            self.assertEqual(post(self.client, make_body(contact={'message': f'quote {n}'})).status_code, 201)

        response = post(self.client, make_body(contact={'message': 'quote 4'}))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['detail'],
                         "We already have your request. We'll reply soon, or call +1 202-247-0837.")
        self.assertGreater(int(response['Retry-After']), 3000)
        self.assertEqual(QuoteRejection.objects.get().reason, 'throttle_email')

        QuoteSubmission.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(post(self.client, make_body(contact={'message': 'quote 5'})).status_code, 201)

    def test_email_throttle_ignores_duplicates(self):
        first = make_body(contact={'message': 'first'})
        self.assertEqual(post(self.client, first).status_code, 201)
        for _ in range(4):
            response = post(self.client, first)                                   # row 6
            self.assertEqual((response.status_code, response.json()['duplicate']), (200, True))
            response = post(self.client, {**first, 'submission_id': make_body()['submission_id']})  # row 8
            self.assertEqual((response.status_code, response.json()['duplicate']), (200, True))

        for n in range(2):
            self.assertEqual(post(self.client, make_body(contact={'message': f'new {n}'})).status_code, 201)
        self.assertEqual(post(self.client, make_body(contact={'message': 'one too many'})).status_code, 429)
        self.assertEqual(QuoteSubmission.objects.count(), 3)


class ProxyHopTests(NoNetworkTestCase):
    def test_ident_uses_configured_hop(self):
        request = RequestFactory().post('/api/quote-requests/', HTTP_X_FORWARDED_FOR='203.0.113.7, 198.51.100.2',
                                        REMOTE_ADDR='10.1.1.1')
        throttle = antispam.QuoteBurstThrottle()
        for hops, ident in ((1, '198.51.100.2'), (2, '203.0.113.7'), (5, '203.0.113.7'), (0, '10.1.1.1')):
            with self.subTest(hops=hops), override_settings(QUOTES_PROXY_HOPS=hops):
                self.assertEqual(throttle.get_ident(request), ident)

        spoofed = RequestFactory().post('/', HTTP_X_FORWARDED_FOR='not-an-ip', REMOTE_ADDR='10.1.1.1')
        self.assertEqual(antispam.client_ip(spoofed), None)
        self.assertEqual(throttle.get_ident(spoofed), 'unknown')
        self.assertEqual(antispam.QuoteSustainedThrottle().get_ident(request), '198.51.100.2')


@enabled
class SuspicionTests(NoNetworkTestCase):
    def test_two_signals_suspect(self):
        message = 'see https://a.example https://b.example www.c.example'
        body = make_body(contact={'email': 'maria@mailinator.com', 'message': message})
        self.assertEqual(antispam.suspicion(normalised(body), 'skipped'),
                         ('suspect', ['message_urls', 'disposable_email']))

        response = post(self.client, body)
        self.assertEqual(response.status_code, 201)
        quote = QuoteSubmission.objects.get()
        self.assertEqual((quote.spam_status, quote.spam_reasons), ('suspect', ['message_urls', 'disposable_email']))
        self.assertEqual(quote.sync_status, 'pending')

        one_signal = make_body(contact={'message': message})
        self.assertEqual(antispam.suspicion(normalised(one_signal), 'passed'), ('clean', ['message_urls']))

    def test_skipped_turnstile_is_not_a_signal(self):
        body = make_body()
        del body['timing']
        nq = normalised(body)
        self.assertEqual(antispam.suspicion(nq, 'skipped'), ('clean', ['no_timing']))
        self.assertEqual(antispam.suspicion(nq, 'passed'), ('clean', ['no_timing']))
        self.assertEqual(antispam.suspicion(nq, 'missing'), ('suspect', ['turnstile_missing', 'no_timing']))
        self.assertEqual(antispam.suspicion(nq, 'error'), ('suspect', ['turnstile_error', 'no_timing']))


@enabled
@override_settings(QUOTES_HOURLY_CEILING=2, OPS_ALERT_EMAILS=['ops@example.com'])
class CircuitTests(NoNetworkTestCase):
    def test_over_ceiling_held_no_publish(self):
        make_quote_submission()
        make_quote_submission()

        with patch('quotes.tasks.process_quote_submission.delay') as process, \
                patch('quotes.tasks.send_quote_staff_email.apply_async') as staff:
            with self.captureOnCommitCallbacks(execute=True):
                response = post(self.client, make_body())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['confirmation_email'], 'off')
        quote = QuoteSubmission.objects.get(reference=response.json()['reference'])
        self.assertEqual((quote.spam_status, quote.sync_status, quote.staff_email_status),
                         ('held', 'held', 'skipped'))
        self.assertIn('hourly_ceiling', quote.spam_reasons)
        process.assert_not_called()
        staff.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('hourly ceiling', mail.outbox[0].subject)
        self.assertNotIn('maria', mail.outbox[0].body.lower())

        QuoteSubmission.objects.update(created_at=timezone.now() - timedelta(hours=2))
        response = post(self.client, make_body(contact={'message': 'next hour'}))
        self.assertEqual(QuoteSubmission.objects.get(reference=response.json()['reference']).spam_status, 'clean')


@enabled
@with_secret
class ReplayTests(NoNetworkTestCase):
    def test_duplicate_submission_skips_turnstile(self):
        body = make_body(turnstile_token='single-use')
        self.http.queue(PASS)
        first = post(self.client, body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(len(siteverify_calls(self.http)), 1)

        cache.clear()   # the cached pass is not what saves the retry here: row 6 answers before Turnstile
        again = post(self.client, body)

        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), {**first.json(), 'duplicate': True})
        self.assertEqual(len(siteverify_calls(self.http)), 1)
        self.assertEqual(QuoteSubmission.objects.count(), 1)

    def test_soft_dedupe_skips_turnstile(self):
        body = make_body(turnstile_token='first-token')
        self.http.queue(PASS)
        first = post(self.client, body)
        self.assertEqual(first.status_code, 201)

        lost_session = {**body, 'submission_id': make_body()['submission_id'], 'turnstile_token': 'second-token'}
        again = post(self.client, lost_session)

        self.assertEqual(again.status_code, 200)
        self.assertEqual((again.json()['duplicate'], again.json()['reference']), (True, first.json()['reference']))
        self.assertEqual(len(siteverify_calls(self.http)), 1)
        self.assertEqual(QuoteSubmission.objects.count(), 1)

    def test_retry_after_crash_reuses_cached_turnstile_pass(self):
        body = make_body(turnstile_token='single-use')
        self.http.queue(PASS)
        with patch('quotes.views.save_submission', side_effect=RuntimeError('database went away')):
            crashed = post(self.client, body)
        self.assertEqual(crashed.status_code, 500)
        self.assertEqual(crashed.json()['detail'], 'server_error')
        self.assertFalse(QuoteSubmission.objects.exists())

        retry = post(self.client, body)   # a second siteverify would answer timeout-or-duplicate

        self.assertEqual(retry.status_code, 201)
        self.assertEqual(len(siteverify_calls(self.http)), 1)
        self.assertEqual(QuoteSubmission.objects.get().turnstile_result, 'passed')

    def test_order_of_checks_matches_design_table(self):
        """One subTest per row of DESIGN §3.5: each check fires although every later check would fire too."""
        too_big = make_body()
        too_big['contact']['message'] = 'x' * 70000

        def exhaust_ip(ip):
            throttle = antispam.QuoteBurstThrottle()
            cache.set(throttle.cache_format % {'scope': throttle.scope, 'ident': ip}, [timezone.now().timestamp()] * 5)

        def saved(body):
            self.http.queue(PASS)
            response = post(self.client, body)
            self.assertEqual(response.status_code, 201)
            return response.json()['reference']

        invalid = {'contact': {'email': 'bad'}}
        rows = []

        # 1: disabled beats everything, even an oversized body.
        with override_settings(QUOTE_V3_ENABLED=False):
            rows.append((1, post(self.client, too_big).status_code, 503))
        # 2: size beats the IP throttle.
        ip = next_ip()
        exhaust_ip(ip)
        rows.append((2, post(self.client, too_big, ip=ip).status_code, 413))
        # 3: the IP throttle beats the honeypot.
        rows.append((3, post(self.client, make_body(fax='x'), ip=ip).status_code, 429))
        # 4: the honeypot beats the time trap and validation.
        trap = make_body(fax='x', timing={'elapsed_ms': 10}, contact={'email': 'bad'})
        response = post(self.client, trap)
        rows.append((4, (response.status_code, QuoteRejection.objects.latest('id').reason), (200, 'honeypot')))
        # 5: the time trap beats validation.
        response = post(self.client, make_body(timing={'elapsed_ms': 10}, contact={'email': 'bad'}))
        rows.append((5, (response.status_code, QuoteRejection.objects.latest('id').reason), (200, 'time_trap')))
        # 6: a known submission_id beats validation, the test-only gate and Turnstile.
        original = make_body(contact={'message': 'row 6'}, turnstile_token='t6')
        reference = saved(original)
        calls = len(siteverify_calls(self.http))
        with override_settings(QUOTE_V3_TEST_ONLY=True):
            response = post(self.client, {**original, 'contact': {**original['contact'], 'email': 'bad'}})
        rows.append((6, (response.status_code, response.json().get('reference'), len(siteverify_calls(self.http))),
                     (200, reference, calls)))
        # 7: validation beats the test-only gate.
        with override_settings(QUOTE_V3_TEST_ONLY=True):
            response = post(self.client, make_body(contact=invalid['contact']))
        rows.append((7, response.status_code, 400))
        # 8: the soft dedupe beats the test-only gate and Turnstile.
        with override_settings(QUOTE_V3_TEST_ONLY=True):
            response = post(self.client, {**original, 'submission_id': make_body()['submission_id']})
        rows.append((8, (response.status_code, response.json().get('reference')), (200, reference)))
        # 9: the test-only gate beats Turnstile (no siteverify call).
        calls = len(siteverify_calls(self.http))
        with override_settings(QUOTE_V3_TEST_ONLY=True):
            response = post(self.client, make_body(contact={'message': 'row 9'}, turnstile_token='t9'))
        rows.append((9, (response.status_code, len(siteverify_calls(self.http))), (503, calls)))
        # 10: Turnstile beats the email throttle.
        for n in range(3):
            saved(make_body(contact={'email': 'busy@example.com', 'message': f'row 10 #{n}'}, turnstile_token=f'x{n}'))
        self.http.queue(FAIL)
        response = post(self.client, make_body(contact={'email': 'busy@example.com', 'message': 'row 10'},
                                               turnstile_token='bad'))
        rows.append((10, response.status_code, 403))
        # 11: the email throttle beats saving.
        self.http.queue(PASS)
        response = post(self.client, make_body(contact={'email': 'busy@example.com', 'message': 'row 11'},
                                               turnstile_token='ok'))
        rows.append((11, response.status_code, 429))
        # 12: suspicion saves with spam_status=suspect.
        self.http.queue(PASS)
        response = post(self.client, make_body(contact={'email': 'x@mailinator.com', 'message': 'row 12'},
                                               timing=None, turnstile_token='t12'))
        rows.append((12, (response.status_code, _spam(response)), (201, 'suspect')))
        # 13: the circuit holds the quote, suspect or not.
        with override_settings(QUOTES_HOURLY_CEILING=QuoteSubmission.objects.count()):
            self.http.queue(PASS)
            response = post(self.client, make_body(contact={'email': 'y@mailinator.com', 'message': 'row 13'},
                                                   timing=None, turnstile_token='t13'))
        rows.append((13, (response.status_code, _spam(response)), (201, 'held')))

        self.assertEqual([row for row, _actual, _expected in rows], list(range(1, 14)))
        for row, actual, expected in rows:
            with self.subTest(row=row):
                self.assertEqual(actual, expected)


def _spam(response) -> str | None:
    reference = response.json().get('reference')
    return QuoteSubmission.objects.get(reference=reference).spam_status if reference else None
