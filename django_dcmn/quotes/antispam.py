"""Anti-spam and replay checks of `POST /api/quote-requests/`, in the order of DESIGN §3.5.

    row 1   QUOTE_V3_ENABLED off → 503                              the view
    row 2   body > 64 KB → 413 (needs the raw bytes)                read_body
    row 3-5 IP throttles, honeypot, time trap                       early_checks
    row 6   replay lookup by submission_id (no external call)       replay_lookup
    row 7   validation                                              quotes.validation.normalise
    row 8   soft content dedupe                                     quotes.intake.soft_dedupe
    row 9-11 test-only gate, Turnstile, per-email throttle          late_checks
    row 12  suspicion score                                         suspicion
    row 13  hourly circuit                                          circuit_tripped

A client retry is answered by rows 6 and 8 before anything a retry could fail: Turnstile tokens are single-use
and the per-email throttle would count the retry. Logs carry reasons and codes only (invariant 9).
"""

import hashlib
import ipaddress
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.throttling import AnonRateThrottle

from . import flags
from .contract import FALLBACK, MAX_BODY_BYTES
from .models import QuoteRejection, QuoteSubmission
from .validation import NormalisedQuote, clean_text, parse_uuid, short_text

logger = logging.getLogger(__name__)

HONEYPOT_FIELDS = ('website_confirm', 'fax')   # the names of orders/views/applications.py
TIME_TRAP_MS = 3000
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
TURNSTILE_TIMEOUT = (2, 3)
TURNSTILE_PASS_TTL_S = 15 * 60
# siteverify codes that mean our side is misconfigured, not the visitor: treated as `error` (allowed)
TURNSTILE_SERVER_ERRORS = frozenset({'missing-input-secret', 'invalid-input-secret', 'internal-error'})
EMAIL_LIMITS = ((timedelta(hours=1), 3), (timedelta(days=1), 10))
CIRCUIT_WINDOW = timedelta(hours=1)
SUSPECT_SIGNALS = 2
MESSAGE_URLS_MAX = 2
MAX_INT_FIELD = 2_147_483_647

TURNSTILE_DETAIL = 'Verification failed, please reload the page and try again.'
EMAIL_THROTTLE_DETAIL = "We already have your request. We'll reply soon, or call +1 202-247-0837."

_URL = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)
DISPOSABLE_DOMAINS = frozenset({
    '10minutemail.com', 'dispostable.com', 'emailondeck.com', 'fakeinbox.com', 'getnada.com', 'guerrillamail.com',
    'guerrillamail.net', 'maildrop.cc', 'mailinator.com', 'mailnesia.com', 'mintemail.com', 'mohmal.com',
    'sharklasers.com', 'spamgourmet.com', 'temp-mail.org', 'tempmail.com', 'tempmailo.com', 'throwawaymail.com',
    'trashmail.com', 'yopmail.com',
})

TurnstileResult = Literal['passed', 'failed', 'missing', 'error', 'skipped']


@dataclass
class Verdict:
    action: Literal['allow', 'fake_ok', 'reject', 'duplicate']
    status: int = 200
    body: dict | None = None
    reason: str = ''


ALLOW = Verdict('allow')


def received_at(moment: datetime | None = None) -> str:
    """`2026-11-26T15:42:32Z` (UTC, seconds)."""
    return (moment or timezone.now()).astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def unavailable_body() -> dict:
    return {'ok': False, 'detail': 'unavailable', 'fallback': FALLBACK}


def too_large() -> Verdict:
    return Verdict('reject', 413, {'ok': False, 'detail': 'too_large'}, 'too_large')


# --- Rows 2-5 ------------------------------------------------------------------------------------

def read_body(request) -> bytes | None:
    """Row 2: the raw body, or None when it is larger than 64 KB (checked before parsing)."""
    try:
        declared = int(request.META.get('CONTENT_LENGTH') or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        return None
    try:
        raw = request.body
    except Exception:   # RequestDataTooBig and friends: Django refused to read it
        return None
    return None if len(raw) > MAX_BODY_BYTES else raw


def client_ip(request) -> str | None:
    """The X-Forwarded-For entry QUOTES_PROXY_HOPS from the right (added by our proxy), else REMOTE_ADDR.

    For the quote views only; the global NUM_PROXIES is untouched (DESIGN §3.5 row 3, ASSUMPTION A6).
    """
    hops = flags.proxy_hops()
    forwarded = [part.strip() for part in request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if part.strip()]
    candidate = forwarded[-min(hops, len(forwarded))] if hops and forwarded else request.META.get('REMOTE_ADDR', '')
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


class _QuoteThrottle(AnonRateThrottle):
    def get_ident(self, request):
        return client_ip(request) or 'unknown'


class QuoteBurstThrottle(_QuoteThrottle):
    scope = 'quotes_burst'


class QuoteSustainedThrottle(_QuoteThrottle):
    scope = 'quotes_sustained'


def early_checks(request, body: dict) -> Verdict:
    """Rows 3-5: IP throttles, honeypot, time trap. A trap answers a fake 200 and stores a hashed rejection."""
    for throttle in (QuoteBurstThrottle(), QuoteSustainedThrottle()):
        if not throttle.allow_request(request, None):
            retry_after = max(1, math.ceil(throttle.wait() or 60))
            logger.info('quote_throttled scope=%s', throttle.scope)
            return Verdict('reject', 429, {'ok': False, 'detail': 'throttled', 'retry_after': retry_after},
                           'throttle_ip')
    if any(str(body.get(field) or '').strip() for field in HONEYPOT_FIELDS):
        return _fake_ok(request, body, 'honeypot')
    elapsed = _elapsed_ms(body)
    if elapsed is not None and elapsed < TIME_TRAP_MS:
        return _fake_ok(request, body, 'time_trap')
    return ALLOW


def _fake_ok(request, body: dict, reason: str) -> Verdict:
    record_rejection(reason, request, body)
    return Verdict('fake_ok', 200, {'ok': True, 'reference': None, 'confirmation_email': 'off',
                                    'received_at': received_at()}, reason)


# --- Row 6 ---------------------------------------------------------------------------------------

def replay_lookup(body: dict) -> QuoteSubmission | None:
    """Row 6: the saved submission with this submission_id (a plain indexed read, no external call)."""
    submission_id = parse_uuid(body.get('submission_id'))
    if submission_id is None:
        return None
    return QuoteSubmission.objects.filter(submission_id=submission_id).first()


# --- Rows 9-11 -----------------------------------------------------------------------------------

def late_checks(request, nq: NormalisedQuote, body: dict) -> tuple[Verdict, str]:
    """Rows 9-11: test-only gate, Turnstile, per-email throttle. Returns (verdict, turnstile_result)."""
    if flags.v3_test_only() and not nq.core['is_test']:
        return Verdict('reject', 503, unavailable_body(), 'test_only'), ''

    result = verify_turnstile(body.get('turnstile_token'), client_ip(request), str(nq.core['submission_id']))
    if result == 'failed' or (result == 'missing' and flags.turnstile_required()):
        record_rejection('turnstile', request, body)
        return Verdict('reject', 403, {'ok': False, 'detail': TURNSTILE_DETAIL, 'errors': {'turnstile': [result]}},
                       'turnstile'), result

    retry_after = email_throttle_wait(nq.core['email_normalized'])
    if retry_after:
        record_rejection('throttle_email', request, body)
        return Verdict('reject', 429, {'ok': False, 'detail': EMAIL_THROTTLE_DETAIL, 'retry_after': retry_after},
                       'throttle_email'), result
    return ALLOW, result


def verify_turnstile(token: str | None, ip: str | None, submission_id: str) -> TurnstileResult:
    """Cloudflare siteverify with (2, 3) s timeouts; `orders.services.applications.verify_turnstile` is not used.

    No secret → skipped. No token → missing (the caller rejects it only with QUOTE_TURNSTILE_REQUIRED).
    Cloudflare unreachable or 5xx → error (allowed). success=false → failed. A pass is cached for 15 min per
    submission_id + token, so a retry after a crash before the insert does not hit `timeout-or-duplicate`.
    """
    secret = getattr(settings, 'TURNSTILE_SECRET_KEY', '') or ''
    if not secret:
        return 'skipped'
    if not token or not isinstance(token, str):
        return 'missing'
    pass_key = f'quotes:ts:{submission_id}:{hashlib.sha256(token.encode()).hexdigest()[:16]}'
    if cache.get(pass_key):
        return 'passed'
    data = {'secret': secret, 'response': token}
    if ip:
        data['remoteip'] = ip
    try:
        response = requests.post(TURNSTILE_VERIFY_URL, data=data, timeout=TURNSTILE_TIMEOUT)
        payload = response.json() if response.status_code < 500 else None
    except (requests.RequestException, ValueError):
        payload = None
    if not isinstance(payload, dict):
        logger.warning('turnstile_unavailable')
        return 'error'
    if payload.get('success') is True:
        cache.set(pass_key, 1, TURNSTILE_PASS_TTL_S)
        return 'passed'
    codes = [str(code) for code in payload.get('error-codes') or []]
    logger.info('turnstile_rejected codes=%s', ','.join(codes))
    return 'error' if TURNSTILE_SERVER_ERRORS.intersection(codes) else 'failed'


def email_throttle_wait(email_normalized: str) -> int:
    """Seconds until another quote from this email is accepted (3/h, 10/day); 0 when allowed.

    Counts saved quotes only, so the duplicates answered by rows 6 and 8 never count.
    """
    now = timezone.now()
    wait = 0.0
    for window, limit in EMAIL_LIMITS:
        times = list(QuoteSubmission.objects.filter(
            email_normalized=email_normalized, created_at__gte=now - window,
        ).order_by('created_at').values_list('created_at', flat=True))
        if len(times) >= limit:
            wait = max(wait, (times[len(times) - limit] + window - now).total_seconds())
    return max(1, math.ceil(wait)) if wait > 0 else 0


# --- Rows 12-13 ----------------------------------------------------------------------------------

def suspicion(nq: NormalisedQuote, turnstile_result: str) -> tuple[str, list[str]]:
    """Row 12: ('suspect' | 'clean', signals). Two signals make a suspect; `skipped` Turnstile is not one."""
    core = nq.core
    signals = []
    if len(_URL.findall(core['message'])) > MESSAGE_URLS_MAX:
        signals.append('message_urls')
    if _URL.search(core['full_name']):
        signals.append('url_in_name')
    if turnstile_result not in ('passed', 'skipped'):
        signals.append(f'turnstile_{turnstile_result or "none"}')
    if core['email_normalized'].rsplit('@', 1)[-1] in DISPOSABLE_DOMAINS:
        signals.append('disposable_email')
    if core['timing'].get('elapsed_ms') is None:
        signals.append('no_timing')
    return ('suspect' if len(signals) >= SUSPECT_SIGNALS else 'clean'), signals


def circuit_tripped() -> bool:
    """Row 13: more than QUOTES_HOURLY_CEILING quotes accepted in the last hour (this one would exceed it)."""
    since = timezone.now() - CIRCUIT_WINDOW
    return QuoteSubmission.objects.filter(created_at__gte=since).count() >= flags.hourly_ceiling()


def confirmation_blocked(spam_status: str, spam_reasons) -> bool:
    """No client confirmation for a suspect quote or a name carrying a URL: the public form must never relay a
    branded email with someone's text to an address they typed in."""
    return spam_status in ('held', 'spam', 'suspect') or 'url_in_name' in (spam_reasons or [])


def has_url(text: str | None) -> bool:
    return bool(_URL.search(text or ''))


# --- Rejection rows ------------------------------------------------------------------------------

def pii_hash(value: str | None) -> str:
    """Salted SHA-256 (PII_HASH_SALT); empty for an empty value."""
    if not value:
        return ''
    return hashlib.sha256(f'{flags.pii_hash_salt()}:{value}'.encode('utf-8')).hexdigest()


def record_rejection(reason: str, request, body: dict) -> QuoteRejection:
    contact = body.get('contact') if isinstance(body.get('contact'), dict) else {}
    page = body.get('page') if isinstance(body.get('page'), dict) else {}
    elapsed = _elapsed_ms(body)
    rejection = QuoteRejection.objects.create(
        reason=reason,
        ip_hash=pii_hash(client_ip(request)),
        email_hash=pii_hash(clean_text(contact.get('email')).lower()),
        elapsed_ms=elapsed if elapsed is not None and elapsed <= MAX_INT_FIELD else None,
        form_version=short_text(body.get('form_version'), 40),
        page_path=short_text(page.get('path'), 255),
    )
    logger.info('quote_rejected reason=%s id=%s', reason, rejection.pk)
    return rejection


def _elapsed_ms(body: dict) -> int | None:
    timing = body.get('timing') if isinstance(body.get('timing'), dict) else {}
    elapsed = timing.get('elapsed_ms')
    return elapsed if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0 else None
