"""Ops alerts by email (DESIGN §12.2) and the beat heartbeat check (§12.1).

Alerts carry references, ids, codes and admin links only (invariant 9). As a last guard, anything that looks
like an email address or a phone number is masked before sending.
"""

import contextvars
import logging
import re
import time
from contextlib import contextmanager
from typing import Iterator

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMessage, get_connection

from . import flags

logger = logging.getLogger(__name__)

ALERT_DEDUPE_S = 3600
# Inside a Zoho button request the alert send must not push the reply past Zoho's 10 s function limit (the
# Resend backend waits 30 s by default). Background alerts keep the default.
FAST_SEND_TIMEOUT = (1.0, 2.0)
_fast_sends: contextvars.ContextVar[bool] = contextvars.ContextVar('crm_alerts_fast_sends', default=False)


@contextmanager
def fast_sends() -> Iterator[None]:
    """Alerts sent inside this block use FAST_SEND_TIMEOUT."""
    token = _fast_sends.set(True)
    try:
        yield
    finally:
        _fast_sends.reset(token)
SUBJECT_PREFIX = '[DCMN ops]'

BEAT_HEARTBEAT_KEY = 'crm:beat:hb'   # written every 60 s by crm.tasks.beat_heartbeat
BEAT_CHECK_KEY = 'crm:beat:check'
BEAT_CHECK_EVERY_S = 5 * 60
BEAT_STALE_AFTER_S = 15 * 60
BEAT_FIRST_CHECK_KEY = 'crm:beat:first_check'   # when a check first found no heartbeat (the grace period)

_WHITESPACE_RE = re.compile(r'\s+')
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# +E.164 / international, or a US number with optional separators; never inside a longer digit run or a URL
# path, so Zoho record ids (19 digits) and references such as Q-482913 stay readable.
_PHONE_RE = re.compile(
    r'(?<![\w/+])(?:\+\d[\d\s().-]{5,18}\d|(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4})(?![\w/])'
)


def alert_ops(kind: str, key: str, subject: str, body: str) -> bool:
    """Email OPS_ALERT_EMAILS (fallback EMAIL_OFFICE_RECEIVER) once per (kind, key) per hour.

    Returns False when the alert was deduplicated, had no recipient or could not be sent.
    """
    recipients = flags.ops_alert_emails()
    if not recipients:
        logger.warning('ops_alert_no_recipient kind=%s', kind)
        return False
    dedupe_key = _WHITESPACE_RE.sub('_', f'alert:{kind}:{key}')   # a valid key for every cache backend
    if not cache.add(dedupe_key, 1, ALERT_DEDUPE_S):
        return False
    message = EmailMessage(
        subject=f'{SUBJECT_PREFIX} {scrub(subject)}',
        body=scrub(body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    fast = _fast_sends.get()
    try:
        if fast:
            message.connection = get_connection(requests_timeout=FAST_SEND_TIMEOUT)
        message.send()
    except Exception:
        if fast and _send_later(message):   # too slow inside a button call: a worker sends it
            logger.warning('ops_alert_deferred kind=%s key=%s', kind, scrub(key))
            return True
        cache.delete(dedupe_key)  # the next occurrence tries again
        logger.exception('ops_alert_send_failed kind=%s key=%s', kind, scrub(key))
        return False
    logger.info('ops_alert_sent kind=%s key=%s', kind, scrub(key))
    return True


def _send_later(message: EmailMessage) -> bool:
    from .tasks.ops import send_ops_alert   # the tasks import this module

    try:
        send_ops_alert.delay(message.subject, message.body, list(message.to))
    except Exception:
        logger.exception('ops_alert_defer_failed')
        return False
    return True


def scrub(text: str) -> str:
    """Mask email addresses and phone numbers."""
    return _PHONE_RE.sub('[phone]', _EMAIL_RE.sub('[email]', str(text)))


def check_beat() -> bool:
    """Alert `reconciler_stale` when the beat heartbeat is missing or older than 15 min.

    Runs at most once per 5 min (callers: the config endpoint, the engine API, the staff-email task).
    Returns True when an alert was sent.
    """
    if not cache.add(BEAT_CHECK_KEY, 1, BEAT_CHECK_EVERY_S):
        return False
    beat_at = cache.get(BEAT_HEARTBEAT_KEY)
    if beat_at is None:
        # No heartbeat yet (a fresh deploy of the schedule, a Redis restart): stale only after the grace period.
        cache.add(BEAT_FIRST_CHECK_KEY, time.time(), None)
        missing_for = time.time() - float(cache.get(BEAT_FIRST_CHECK_KEY) or time.time())
        if missing_for <= BEAT_STALE_AFTER_S:
            return False
        seen = f'no heartbeat recorded for {int(missing_for)} s'
    else:
        cache.delete(BEAT_FIRST_CHECK_KEY)
        age = time.time() - float(beat_at)
        if age <= BEAT_STALE_AFTER_S:
            return False
        seen = f'last heartbeat {int(age)} s ago'
    return alert_ops(
        'reconciler_stale', 'beat',
        'Celery beat looks stopped',
        f'The beat heartbeat ({BEAT_HEARTBEAT_KEY}) is stale: {seen}.\n'
        'Reconcilers, the Zoho token pre-warm and the daily digest are not running. '
        'Check the Railway beat service.',
    )
