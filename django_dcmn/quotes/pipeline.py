"""Step runner of a v3 submission (DESIGN §5.2): contact → attribution → GAQL record → receipt Note →
phone link → intake.

A run claims the row with a conditional UPDATE (the lease), makes its HTTP calls outside any DB transaction
and saves each step's result right after its call, so a re-run skips what is done. Transient errors go to
`retry` on the backoff ladder, permanent ones to `failed` + alert; the staff email already carries the
request, so a lead is never lost. Every Zoho call goes through `crm.zoho` with `trigger: []`.
"""

import logging
import traceback
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm import flags as crm_flags
from crm import zoho
from crm.alerts import alert_ops
from crm.contacts import differences as contact_differences
from crm.contacts import find_or_create
from orders.services.attribution import build_zoho_attribution_payload
from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

from . import emails, flags, gaql, phone_match
from .models import GAQL_MODULE, QuoteSubmission
from .render import RECEIPT_MAX, receipt_text

logger = logging.getLogger(__name__)

STEPS = ('contact', 'attribution', 'record', 'details_note', 'phone_link', 'intake', 'done')
BACKOFF = (30, 120, 600, 1800, 3600, 7200, 14400, 28800)
MAX_AGE = timedelta(hours=24)     # retry window, counted on the ladder (a re-armed row gets a full one)
LEASE = timedelta(minutes=5)

NON_BLOCKING = frozenset({'attribution', 'details_note', 'phone_link'})
NON_BLOCKING_TRIES = 3            # then the step is skipped with a warning
BUG_TRIES = 3                     # an unexpected exception: transient for 3 tries, then failed
FAILURE_COUNT_TTL_S = 2 * 24 * 3600
ERROR_MAX = 2000

ATTRIBUTION_MODULE = 'Lead_Attribution_Records'
ATTRIBUTION_URL_MAX = 255
SPAM_TAG = 'possible-spam'
AMENDMENT_VALUE_MAX = 200

RECONCILE_BATCH = 100
STAFF_EMAIL_WATCHDOG = timedelta(minutes=3)
EMAIL_SENDING_STALE = timedelta(minutes=10)   # a worker died between claim and send
UNSYNCED_ALERT_AFTER = timedelta(minutes=30)
AMENDMENT_NOTE_WINDOW = timedelta(hours=25)


class BlockedPayload(Exception):
    """The schema guard dropped a blocking field (DESIGN §7.10): never written, never a fallback value."""

    code = 'SCHEMA_BLOCKED'

    def __init__(self, fields: list[str]):
        super().__init__(f'blocked: {",".join(fields)}')
        self.field = ','.join(fields)


# --- Lease and run --------------------------------------------------------------------------------

def claim(pk: int) -> bool:
    """Take the 5-minute lease; False when another worker owns the row or it is not runnable."""
    now = timezone.now()
    return QuoteSubmission.objects.filter(
        Q(sync_lease_until__isnull=True) | Q(sync_lease_until__lt=now),
        pk=pk, sync_status__in=('pending', 'retry'),
    ).update(sync_status='running', sync_lease_until=now + LEASE, updated_at=now) == 1


def run(pk: int) -> str:
    """Run the remaining steps of one submission; returns its final sync_status."""
    if not flags.zoho_sync():
        # Store-and-forward: the reconciler re-arms the row once QUOTE_V3_ZOHO_SYNC is back on.
        QuoteSubmission.objects.filter(pk=pk, sync_status__in=('pending', 'retry')).update(
            sync_status='paused', updated_at=timezone.now())
        return _status(pk)
    if not claim(pk):
        return _status(pk)
    quote = QuoteSubmission.objects.get(pk=pk)
    for step in STEPS[:-1]:
        if is_done(quote, step):
            continue
        outcome = _run_step(quote, step)
        if outcome is not None:
            return outcome
    _save_if_running(quote, sync_status='synced', sync_step='done', synced_at=timezone.now(),
                     sync_lease_until=None, sync_next_at=None, sync_last_error='')
    logger.info('quote_synced id=%s record=%s', quote.pk, quote.zoho_record_id)
    return quote.sync_status


def _status(pk: int) -> str:
    return QuoteSubmission.objects.filter(pk=pk).values_list('sync_status', flat=True).first() or 'missing'


def is_done(quote: QuoteSubmission, step: str) -> bool:
    """A step is skipped when its result is persisted or a later step already completed."""
    if quote.sync_step in STEPS and STEPS.index(quote.sync_step) >= STEPS.index(step):
        return True
    return {
        'contact': bool(quote.zoho_contact_action),
        'attribution': bool(quote.zoho_attribution_id),
        'record': bool(quote.zoho_record_id),
        'details_note': bool(quote.zoho_note_id),
    }.get(step, False)


def _run_step(quote: QuoteSubmission, step: str) -> str | None:
    """None when the run goes on; else the status the run ended with."""
    try:
        STEP_FUNCTIONS[step](quote)
    except zoho.CircuitOpen as exc:   # no HTTP was made: not a failure of the step
        return _retry(quote, step, exc)
    except ZohoTransientError as exc:
        if step in NON_BLOCKING and _give_up(quote, step, exc):
            return None
        return _retry(quote, step, exc)
    except (ZohoPermanentError, BlockedPayload) as exc:
        if step in NON_BLOCKING:
            _skip(quote, step, exc)
            return None
        return _fail(quote, step, exc)
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        logger.error('quote_step_error id=%s step=%s error=%s at=%s:%s', quote.pk, step, type(exc).__name__,
                     frame.filename.rsplit('/', 1)[-1], frame.lineno)
        if step in NON_BLOCKING and _give_up(quote, step, exc):
            return None
        if quote.sync_attempts + 1 >= BUG_TRIES:
            return _fail(quote, step, exc)
        return _retry(quote, step, exc)
    _save(quote, sync_step=step)
    return None


def _give_up(quote: QuoteSubmission, step: str, exc: Exception) -> bool:
    """Count a failure of a non-blocking step; at the third one the step is skipped."""
    key = f'quotes:step_failures:{quote.pk}:{step}'
    cache.add(key, 0, FAILURE_COUNT_TTL_S)
    if cache.incr(key) < NON_BLOCKING_TRIES:
        return False
    _skip(quote, step, exc)
    return True


def _skip(quote: QuoteSubmission, step: str, exc: Exception) -> None:
    logger.warning('quote_step_skipped id=%s step=%s code=%s', quote.pk, step, _code(exc))
    _save(quote, sync_step=step)


def _retry(quote: QuoteSubmission, step: str, exc: Exception) -> str:
    attempts = quote.sync_attempts + 1
    if retry_window(attempts) > MAX_AGE:
        return _fail(quote, step, exc, attempts=attempts)
    delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
    _save_if_running(quote, sync_status='retry', sync_attempts=attempts, sync_lease_until=None,
                     sync_next_at=timezone.now() + timedelta(seconds=delay), sync_last_error=_error_text(step, exc))
    logger.warning('quote_sync_retry id=%s step=%s code=%s attempt=%s delay_s=%s',
                   quote.pk, step, _code(exc), attempts, delay)
    return quote.sync_status


def retry_window(attempts: int) -> timedelta:
    """Time spent waiting on the ladder after `attempts` failures."""
    return timedelta(seconds=sum(BACKOFF[min(i, len(BACKOFF) - 1)] for i in range(attempts)))


def _fail(quote: QuoteSubmission, step: str, exc: Exception, *, attempts: int | None = None) -> str:
    error = _error_text(step, exc)
    _save_if_running(quote, sync_status='failed', sync_failed_at=timezone.now(), sync_lease_until=None,
                     sync_next_at=None, sync_last_error=error, sync_attempts=attempts or quote.sync_attempts + 1)
    logger.error('quote_sync_failed id=%s step=%s code=%s', quote.pk, step, _code(exc))
    alert_ops(
        'quote_sync_failed', quote.reference,
        f'Quote {quote.reference} was not synced to Zoho',
        f'{quote.reference} stopped at step "{step}": {error}\n'
        'The staff email already holds the full request. Fix the cause, then use "Retry from failed step".\n'
        f'{quote.admin_url}',
    )
    if isinstance(exc, BlockedPayload):
        alert_ops('schema_drift', GAQL_MODULE, f'{GAQL_MODULE}: blocking field missing',
                  f'The GAQL create is blocked: {exc.field} is not writable in the Zoho metadata. '
                  f'Run `manage.py zoho_schema --verify`.\nFirst quote: {quote.reference} {quote.admin_url}')
    return quote.sync_status


def _code(exc: Exception) -> str:
    code = getattr(exc, 'code', '') or type(exc).__name__
    field = getattr(exc, 'field', '')
    return f'{code} ({field})' if field else str(code)


def _error_text(step: str, exc: Exception) -> str:
    """Codes and ids only (invariant 9): the Zoho error code and field, or the exception class."""
    return f'{step}: {_code(exc)}'[:ERROR_MAX]


def _save(quote: QuoteSubmission, **fields) -> None:
    """Persist a few fields without touching the others (the email tasks write the same row)."""
    for name, value in fields.items():
        setattr(quote, name, value)
    QuoteSubmission.objects.filter(pk=quote.pk).update(**fields, updated_at=timezone.now())


def _save_if_running(quote: QuoteSubmission, **fields) -> None:
    """A status change of the run; an admin action taken meanwhile (e.g. Mark spam) wins."""
    updated = QuoteSubmission.objects.filter(pk=quote.pk, sync_status='running').update(
        **fields, updated_at=timezone.now())
    if updated:
        for name, value in fields.items():
            setattr(quote, name, value)
    else:
        logger.warning('quote_run_superseded id=%s', quote.pk)
        quote.sync_status = _status(quote.pk)


# --- Steps ----------------------------------------------------------------------------------------

def step_contact(quote: QuoteSubmission) -> None:
    """Find the Zoho Contact by email (fill blanks only) or create it; an email over 100 chars skips it."""
    contact_id, action = find_or_create(quote.full_name, quote.email, quote.phone_e164 or quote.phone_raw)
    _save(quote, zoho_contact_id=contact_id or '', zoho_contact_action=action)


def attribution_key(quote: QuoteSubmission) -> str:
    return f'dcmn:attribution:qr:{quote.pk}'


def step_attribution(quote: QuoteSubmission) -> None:
    """The Lead_Attribution_Records row, in the legacy Name/key format; none for ZZ TEST unless E2E_ATTRIBUTION."""
    data = quote.attribution_data
    if not data or (quote.is_test and not crm_flags.e2e_attribution()):
        return
    data = dict(data)
    data.setdefault('lead_type', 'form')   # as the legacy process path: 'form' → the picklist's 'Form Submission'
    payload = build_zoho_attribution_payload(data, quote.full_name)
    if not payload:
        return
    if payload.get('Lead_Type') in (None, '', 'Form'):   # never the off-list 'Form' (invariant 8)
        payload['Lead_Type'] = 'Form Submission'
    key = attribution_key(quote)
    payload['Name'] = f'DCMN Attribution {key.removeprefix("dcmn:attribution:")}'
    lead_url = data.get('lead_url') or quote.page_url or payload.get('Lead_URL')
    if lead_url:
        payload['Lead_URL'] = str(lead_url)[:ATTRIBUTION_URL_MAX]
    created = zoho.create_once(ATTRIBUTION_MODULE, payload, key, trigger=[])
    _save(quote, zoho_attribution_id=created.id)


def step_record(quote: QuoteSubmission) -> None:
    """The GAQL record (`Web Quote Request`, `trigger: []`), keyed `dcmn:qr:{pk}`; a suspect quote is tagged."""
    subform = gaql.use_subform()
    result = gaql.guarded_record(quote, subform=subform)
    if result.blocked:
        raise BlockedPayload(result.blocked)
    created = zoho.create_once(GAQL_MODULE, result.payload, gaql.record_key(quote), trigger=[])
    lines_mode = 'subform' if subform and gaql.SUBFORM_FIELD in result.payload else 'text'
    _save(quote, zoho_record_id=created.id, zoho_lines_mode=lines_mode)
    if quote.spam_status == 'suspect':
        try:
            zoho.add_tags(GAQL_MODULE, created.id, [SPAM_TAG])
        except ZohoTransientError as exc:   # the comments block still says "possible spam"
            logger.warning('quote_tag_failed id=%s code=%s', quote.pk, _code(exc))


def receipt_title(quote: QuoteSubmission) -> str:
    return f'{quote.reference} · full request'


def receipt_note(quote: QuoteSubmission) -> str:
    """The full receipt (≤10,000) with the Contact differences, plus what the guard kept out of the fields."""
    notes = (contact_differences(quote.zoho_contact_id, quote.full_name, quote.phone_last10)
             if quote.zoho_contact_action == 'matched' and quote.zoho_contact_id else [])
    dropped = gaql.dropped_text(gaql.guarded_record(quote))
    if not dropped:
        return receipt_text(quote, contact_notes=notes)
    body = receipt_text(quote, limit=RECEIPT_MAX - len(dropped) - 2, contact_notes=notes)
    return f'{body}\n\n{dropped}'[:RECEIPT_MAX]


def step_details_note(quote: QuoteSubmission) -> None:
    """The receipt Note on the GAQL record, once: an existing Note with the same title prefix is reused."""
    notes = zoho.list_notes(GAQL_MODULE, quote.zoho_record_id)
    note_id = find_note(notes, receipt_title(quote)) or zoho.add_note(
        GAQL_MODULE, quote.zoho_record_id, receipt_title(quote), receipt_note(quote))
    _save(quote, zoho_note_id=note_id)
    post_amendment_notes(quote, notes)


def find_note(notes: list[dict], title_prefix: str) -> str:
    return next((str(note['id']) for note in notes if str(note.get('Note_Title') or '').startswith(title_prefix)), '')


def step_phone_link(quote: QuoteSubmission) -> None:
    phone_match.match_before(quote)


def step_intake(quote: QuoteSubmission) -> None:
    """`crm.intake.start_intake` (DESIGN §7.3): idempotent, the ServiceLink row is unique."""
    from crm.intake import start_intake   # crm.intake imports quotes models

    start_intake(quote.pk)


STEP_FUNCTIONS = {
    'contact': step_contact,
    'attribution': step_attribution,
    'record': step_record,
    'details_note': step_details_note,
    'phone_link': step_phone_link,
    'intake': step_intake,
}


# --- Amendments (DESIGN §5.2 step 4) ----------------------------------------------------------------

def amendment_title(quote: QuoteSubmission, number: int) -> str:
    return f'{quote.reference} · re-sent with changes ({number})'


def post_amendment_notes(quote: QuoteSubmission, notes: list[dict] | None = None) -> int:
    """One Note per amendment of a synced quote, once (title prefix + `note_id` in the amendment)."""
    pending = [number for number, entry in enumerate(quote.amendments or [], start=1) if not entry.get('note_id')]
    if not pending or not quote.zoho_record_id:
        return 0
    notes = zoho.list_notes(GAQL_MODULE, quote.zoho_record_id) if notes is None else notes
    for number in pending:
        title = amendment_title(quote, number)
        note_id = find_note(notes, title) or zoho.add_note(
            GAQL_MODULE, quote.zoho_record_id, title, amendment_text(quote, quote.amendments[number - 1]))
        _set_amendment_note(quote, number, note_id)
    return len(pending)


def _set_amendment_note(quote: QuoteSubmission, number: int, note_id: str) -> None:
    with transaction.atomic():
        locked = QuoteSubmission.objects.select_for_update().only('amendments').get(pk=quote.pk)
        amendments = list(locked.amendments or [])
        amendments[number - 1] = {**amendments[number - 1], 'note_id': note_id}
        QuoteSubmission.objects.filter(pk=quote.pk).update(amendments=amendments, updated_at=timezone.now())
    quote.amendments = amendments


def amendment_text(quote: QuoteSubmission, amendment: dict) -> str:
    changed = amendment.get('changed') or []
    rows = [f'The client re-sent the form ({amendment.get("at", "?")}) with changes: '
            f'{", ".join(changed) or "no visible field"}.']
    payload = amendment.get('payload') or {}
    rows += [f'{key}: {_amended_value(payload, key)}' for key in changed]
    rows += ['The record fields keep the first version; confirm with the client before quoting.',
             f'Admin: {quote.admin_url}']
    return '\n'.join(rows)[:RECEIPT_MAX]


def _amended_value(payload: dict, key: str) -> str:
    contact = payload.get('contact') if isinstance(payload.get('contact'), dict) else {}
    services = [s for s in payload.get('services') or [] if isinstance(s, dict)]
    if key.startswith('contact.'):
        value = contact.get(key.removeprefix('contact.'))
    elif key == 'message':
        value = contact.get('message')
    elif key == 'services':
        value = ', '.join(str(s.get('service')) for s in services)
    else:
        service, _, answer = key.partition('.')
        item = next((s for s in services if s.get('service') == service), {})
        value = (item.get('answers') or {}).get(answer) if isinstance(item.get('answers'), dict) else None
    text = '—' if value in (None, '') else ' '.join(str(value).split())
    return text[:AMENDMENT_VALUE_MAX]


# --- Admin and command actions ----------------------------------------------------------------------

def rearm(quote: QuoteSubmission) -> bool:
    """`failed` → `pending` from the failed step (completed steps stay done); True when re-armed."""
    return _arm(QuoteSubmission.objects.filter(pk=quote.pk, sync_status='failed'), quote)


def release(quote: QuoteSubmission) -> bool:
    """`held` → `pending`: the quote goes to Zoho and the office gets its email (spam_status `released`).

    A held quote never had a confirmation; once released it gets the one it would have had (invariant 7).
    """
    updated = QuoteSubmission.objects.filter(pk=quote.pk, sync_status='held').update(
        sync_status='pending', spam_status='released', staff_email_status='pending', sync_attempts=0,
        sync_next_at=None, sync_lease_until=None, updated_at=timezone.now())
    if not updated:
        return False
    quote.refresh_from_db()
    _publish_run(quote.pk)
    _publish_staff_email(quote.pk)
    if emails.confirmation_allowed(quote) and QuoteSubmission.objects.filter(
            pk=quote.pk, confirmation_email_status='off').update(confirmation_email_status='pending'):
        quote.confirmation_email_status = 'pending'
        from .tasks import send_quote_confirmation_email

        _publish(send_quote_confirmation_email, quote.pk)
    return True


def mark_spam(quote: QuoteSubmission) -> bool:
    """Terminal `spam` for a quote that never reached Zoho; a synced quote is left to the managers."""
    rows = QuoteSubmission.objects.filter(pk=quote.pk, zoho_record_id='').exclude(sync_status__in=('running', 'synced'))
    if not rows.update(sync_status='spam', spam_status='spam', sync_lease_until=None, sync_next_at=None,
                       updated_at=timezone.now()):
        return False
    QuoteSubmission.objects.filter(pk=quote.pk, staff_email_status='pending').update(staff_email_status='skipped')
    QuoteSubmission.objects.filter(pk=quote.pk, confirmation_email_status='pending').update(
        confirmation_email_status='skipped')
    quote.refresh_from_db()
    return True


def resend_email(quote: QuoteSubmission, kind: str) -> bool:
    """Admin "Resend staff email" / "Resend confirmation" (`kind` staff|confirmation)."""
    if not emails.rearm(quote, kind):
        return False
    from .tasks import send_quote_confirmation_email, send_quote_staff_email

    return _publish(send_quote_staff_email if kind == 'staff' else send_quote_confirmation_email, quote.pk)


def rerun_intake(quote: QuoteSubmission) -> bool:
    """Admin "Re-run intake": `start_intake` is idempotent; only for a quote that reached Zoho."""
    if not quote.zoho_record_id or quote.spam_status in ('held', 'spam'):
        return False
    from crm.intake import start_intake

    start_intake(quote.pk)
    return True


def _arm(rows, quote: QuoteSubmission) -> bool:
    updated = rows.update(sync_status='pending', sync_attempts=0, sync_next_at=None, sync_lease_until=None,
                          sync_failed_at=None, updated_at=timezone.now())
    if updated:
        cache.delete_many([f'quotes:step_failures:{quote.pk}:{step}' for step in NON_BLOCKING])
        quote.refresh_from_db()
        _publish_run(quote.pk)
    return bool(updated)


def _publish_run(pk: int) -> bool:
    from .tasks import process_quote_submission

    return _publish(process_quote_submission, pk)


def _publish_staff_email(pk: int) -> bool:
    from .tasks import send_quote_staff_email

    return _publish(send_quote_staff_email, pk)


def _publish(task, pk: int) -> bool:
    try:
        task.delay(pk)
    except Exception:
        logger.exception('quote_publish_failed task=%s id=%s', task.name, pk)
        return False
    return True


# --- Reconciler (beat, every 120 s) -------------------------------------------------------------------

def reconcile() -> dict[str, int]:
    """Re-arm paused rows, recover stale leases, republish due rows, watch the emails, alert unsynced rows."""
    from .tasks import send_quote_confirmation_email, send_quote_staff_email

    now = timezone.now()
    counts = {'rearmed': 0, 'recovered': 0, 'published': 0, 'amendments': 0, 'emails_recovered': 0,
              'staff_emails': 0, 'confirmations': 0, 'alerts': 0}
    if flags.zoho_sync():
        counts['rearmed'] = QuoteSubmission.objects.filter(sync_status='paused').update(
            sync_status='pending', sync_next_at=None, updated_at=now)
        counts['recovered'] = QuoteSubmission.objects.filter(sync_status='running', sync_lease_until__lt=now).update(
            sync_status='retry', sync_next_at=now, sync_lease_until=None, updated_at=now)
        due = QuoteSubmission.objects.filter(
            Q(sync_next_at__isnull=True) | Q(sync_next_at__lte=now),
            Q(sync_lease_until__isnull=True) | Q(sync_lease_until__lt=now),
            sync_status__in=('pending', 'retry'),
        ).order_by('created_at').values_list('pk', flat=True)[:RECONCILE_BATCH]
        counts['published'] = sum(_publish_run(pk) for pk in due)
        counts['amendments'] = _amendment_notes(now)

    for field in ('staff_email_status', 'confirmation_email_status'):
        counts['emails_recovered'] += QuoteSubmission.objects.filter(
            **{field: 'sending'}, updated_at__lt=now - EMAIL_SENDING_STALE).update(**{field: 'pending'}, updated_at=now)
    stale = now - STAFF_EMAIL_WATCHDOG
    for pk in _pks(staff_email_status='pending', created_at__lte=stale):
        counts['staff_emails'] += _publish(send_quote_staff_email, pk)
    for pk in _pks(confirmation_email_status='pending', created_at__lte=stale):
        counts['confirmations'] += _publish(send_quote_confirmation_email, pk)
    counts['alerts'] = _alert_unsynced(now)
    return counts


def _pks(**filters) -> list[int]:
    return list(QuoteSubmission.objects.filter(**filters).order_by('created_at')
                .values_list('pk', flat=True)[:RECONCILE_BATCH])


def _amendment_notes(now) -> int:
    posted = 0
    quotes = (QuoteSubmission.objects.filter(sync_status='synced', created_at__gte=now - AMENDMENT_NOTE_WINDOW)
              .exclude(amendments=[]).exclude(zoho_record_id=''))
    for quote in quotes[:RECONCILE_BATCH]:
        try:
            posted += post_amendment_notes(quote)
        except (ZohoTransientError, ZohoPermanentError) as exc:
            logger.warning('quote_amendment_note_failed id=%s code=%s', quote.pk, _code(exc))
    return posted


def _alert_unsynced(now) -> int:
    sent = 0
    rows = QuoteSubmission.objects.filter(
        sync_status__in=('pending', 'running', 'retry'), created_at__lte=now - UNSYNCED_ALERT_AFTER,
        alerted_unsynced_at__isnull=True,
    )
    for quote in rows[:RECONCILE_BATCH]:
        if not QuoteSubmission.objects.filter(pk=quote.pk, alerted_unsynced_at__isnull=True).update(
                alerted_unsynced_at=now):
            continue
        sent += 1
        alert_ops(
            'quote_unsynced_30m', quote.reference,
            f'Quote {quote.reference} not in Zoho after 30 min',
            f'{quote.reference} is still "{quote.sync_status}" (step done: {quote.sync_step or "none"}, '
            f'attempts {quote.sync_attempts}, last error: {quote.sync_last_error or "none"}).\n'
            f'The staff email carries the request. {quote.admin_url}',
        )
    return sent
