"""Saving a submission (DESIGN §3.6, §5.1): soft dedupe, the atomic insert, amendments, publishing.

Nothing here calls an external service: the Celery tasks are published on commit and do the Zoho work.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from crm.alerts import alert_ops
from orders.services.attribution import ATTRIBUTION_FIELD_MAP, clean_attribution_data

from . import antispam, flags
from .models import QuoteLine, QuoteSubmission
from .render import line_summary
from .validation import ContractError, NormalisedQuote, changed_keys, hash_material, normalise, quote_material

logger = logging.getLogger(__name__)

SOFT_DEDUPE_WINDOW = timedelta(minutes=10)
REPEAT_WINDOW = timedelta(hours=24)
AMENDMENT_WINDOW = timedelta(hours=24)
AMENDMENT_LIMIT = 5
REFERENCE_ATTEMPTS = 5
STAFF_EMAIL_COUNTDOWN_S = 20
USER_AGENT_MAX = 500
ATTRIBUTION_TEXT_MAX = 255

# `confirmation_email` of a duplicate answer (DESIGN §3.7); `off` and `skipped` read `off`.
CONFIRMATION_REPLY = {'pending': 'queued', 'sending': 'queued', 'sent': 'sent', 'failed': 'failed'}


@dataclass
class RequestMeta:
    ip: str | None
    user_agent: str
    turnstile_result: str
    attribution: dict | None


@dataclass
class SaveResult:
    quote: QuoteSubmission
    created: bool
    duplicate: bool
    amended: bool


def attribution_from_body(body: dict) -> dict | None:
    """`attribution` cleaned by the legacy `clean_attribution_data`, limited to the fields Zoho knows."""
    cleaned = clean_attribution_data(body.get('attribution') if isinstance(body.get('attribution'), dict) else None)
    if not cleaned:
        return None
    kept = {}
    for key, value in cleaned.items():
        if key not in ATTRIBUTION_FIELD_MAP or not isinstance(value, (str, int, float, bool)):
            continue
        kept[key] = value[:ATTRIBUTION_TEXT_MAX] if isinstance(value, str) else value
    return kept or None


def soft_dedupe(nq: NormalisedQuote) -> QuoteSubmission | None:
    """Row 8: an earlier quote with the same email and core hash in the last 10 minutes."""
    return QuoteSubmission.objects.filter(
        email_normalized=nq.core['email_normalized'],
        payload_core_hash=nq.core_hash,
        created_at__gte=timezone.now() - SOFT_DEDUPE_WINDOW,
    ).order_by('-created_at').first()


def mint_reference() -> str:
    """`Q-` + 6 digits, the first 1–9 (DESIGN §3.8); uniqueness is the database's."""
    return f'Q-{secrets.randbelow(900000) + 100000}'


def save_submission(nq: NormalisedQuote, meta: RequestMeta) -> SaveResult:
    """Rows 12–13, then the atomic insert with the idempotency re-check; publishes the tasks on commit."""
    spam_status, spam_reasons = antispam.suspicion(nq, meta.turnstile_result)
    held = antispam.circuit_tripped()
    if held:
        spam_status, spam_reasons = 'held', [*spam_reasons, 'hourly_ceiling']

    for _attempt in range(REFERENCE_ATTEMPTS):
        existing = None
        try:
            with transaction.atomic():
                existing = find_by_submission_id(nq.core['submission_id'])
                if existing is None:
                    quote = _insert(nq, meta, spam_status, spam_reasons)
        except IntegrityError:
            # A concurrent insert of the same submission_id, or a reference collision (draw again).
            existing = find_by_submission_id(nq.core['submission_id'])
            if existing is None:
                continue
        if existing is not None:
            return SaveResult(quote=existing, created=False, duplicate=True, amended=record_amendment(existing, nq))

        logger.info('quote_saved id=%s spam=%s sync=%s', quote.pk, spam_status, quote.sync_status)
        if held:
            _alert_held(quote)
        transaction.on_commit(lambda: publish(quote.pk), robust=True)
        return SaveResult(quote=quote, created=True, duplicate=False, amended=False)
    raise RuntimeError('quote_reference_exhausted')


def find_by_submission_id(submission_id) -> QuoteSubmission | None:
    return QuoteSubmission.objects.filter(submission_id=submission_id).first()


def _insert(nq: NormalisedQuote, meta: RequestMeta, spam_status: str, spam_reasons: list[str]) -> QuoteSubmission:
    core = nq.core
    held = spam_status == 'held'
    if held:
        sync_status = 'held'
    else:
        sync_status = 'pending' if flags.zoho_sync() else 'paused'
    confirmation = (
        flags.confirmation_email() and not held and not antispam.confirmation_blocked(spam_status, spam_reasons)
        and (not core['is_test'] or flags.v3_test_emails())
    )
    quote = QuoteSubmission.objects.create(
        reference=mint_reference(),
        **core,
        attribution_data=meta.attribution,
        payload_core_hash=nq.core_hash,
        contract_warnings=nq.warnings,
        needs_review=nq.needs_review,
        ip=meta.ip,
        user_agent=meta.user_agent[:USER_AGENT_MAX],
        turnstile_result=meta.turnstile_result,
        spam_status=spam_status,
        spam_reasons=spam_reasons,
        possible_repeat_of=_possible_repeat(core['email_normalized']),
        intake_mode=flags.intake_mode_snapshot(),
        sync_status=sync_status,
        staff_email_status='skipped' if held else 'pending',
        confirmation_email_status='pending' if confirmation else 'off',
    )
    lines = [QuoteLine(quote=quote, **fields) for fields in nq.lines]
    for line in lines:
        line.summary_server = line_summary(line)
    QuoteLine.objects.bulk_create(lines)
    return quote


def _possible_repeat(email_normalized: str) -> QuoteSubmission | None:
    """Repeat flag, not a dedupe (DESIGN §3.6 step 3): the latest quote from this email in the last 24 h."""
    return QuoteSubmission.objects.filter(
        email_normalized=email_normalized, created_at__gte=timezone.now() - REPEAT_WINDOW,
    ).order_by('-created_at').first()


def publish(pk: int) -> None:
    """Queue the step runner and the emails; if the broker is down, send the staff email in-process."""
    from .tasks import process_quote_submission, send_quote_confirmation_email, send_quote_staff_email

    quote = QuoteSubmission.objects.only('sync_status', 'staff_email_status', 'confirmation_email_status').get(pk=pk)
    try:
        if quote.sync_status == 'pending':
            process_quote_submission.delay(pk)
        if quote.staff_email_status == 'pending':
            send_quote_staff_email.apply_async((pk,), countdown=STAFF_EMAIL_COUNTDOWN_S)
        if quote.confirmation_email_status == 'pending':
            send_quote_confirmation_email.delay(pk)
    except Exception:
        # The rows stay pending for the reconciler; the office still hears about the quote now.
        logger.exception('quote_publish_failed id=%s', pk)
        if quote.staff_email_status == 'pending':
            try:
                send_quote_staff_email.apply((pk,))
            except Exception:
                logger.exception('quote_staff_email_inline_failed id=%s', pk)


def _alert_held(quote: QuoteSubmission) -> None:
    hour = timezone.now().strftime('%Y-%m-%dT%H')
    alert_ops(
        'quotes_held', hour,
        'Quote intake held: hourly ceiling reached',
        f'{quote.reference} and later quotes this hour are held (QUOTES_HOURLY_CEILING={flags.hourly_ceiling()}). '
        'Nothing goes to Zoho and no email is sent until a quote is released in the admin.\n'
        f'{quote.admin_url}',
    )


# --- Duplicates and amendments (DESIGN §3.6 step 1) ----------------------------------------------------

def replay(quote: QuoteSubmission, body: dict, *, today: date) -> bool:
    """A re-sent `submission_id`: normalise best-effort and record an amendment when the content changed.

    A validation error on a duplicate is ignored (the original stands). Returns True when an amendment was added.
    """
    try:
        nq = normalise(body, today=today)
    except ContractError:
        return False
    return record_amendment(quote, nq)


def record_amendment(quote: QuoteSubmission, nq: NormalisedQuote) -> bool:
    """Append `{at, core_hash, changed, payload}` when the core hash differs; at most 5 within 24 h."""
    if nq.core_hash == quote.payload_core_hash:
        return False
    with transaction.atomic():
        locked = QuoteSubmission.objects.select_for_update().get(pk=quote.pk)
        amendments = list(locked.amendments or [])
        if any(entry.get('core_hash') == nq.core_hash for entry in amendments):
            return False
        now = timezone.now()
        if len(amendments) >= AMENDMENT_LIMIT or now - locked.created_at > AMENDMENT_WINDOW:
            if 'amendment_limit' not in locked.contract_warnings:
                locked.contract_warnings = [*locked.contract_warnings, 'amendment_limit']
                locked.save(update_fields=['contract_warnings', 'updated_at'])
            return False
        material = hash_material(nq.core, [(line['service_key'], line['answers']) for line in nq.lines])
        amendments.append({
            'at': now.isoformat(),
            'core_hash': nq.core_hash,
            'changed': changed_keys(quote_material(locked), material),
            'payload': nq.core['raw_payload'],
        })
        locked.amendments = amendments
        locked.save(update_fields=['amendments', 'updated_at'])
    quote.amendments = amendments
    logger.info('quote_amended id=%s count=%s', quote.pk, len(amendments))
    return True


def confirmation_reply(quote: QuoteSubmission) -> str:
    return CONFIRMATION_REPLY.get(quote.confirmation_email_status, 'off')
