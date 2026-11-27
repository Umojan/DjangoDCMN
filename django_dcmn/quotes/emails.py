"""Staff email and client confirmation of a v3 submission (DESIGN §5.6).

The staff email keeps the legacy quote subject and daily Gmail thread (must-not-break #7) and carries the
full receipt, so the office has the request even when Zoho is down. The client confirmation is built but
off (`QUOTE_V3_CONFIRMATION_EMAIL`, C12). Both claim `pending → sending` before sending, so a task delivered
twice sends once; three failed sends mark the email `failed` and alert ops.
"""

import logging
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from crm.alerts import alert_ops

from . import contract as c
from . import flags, phone_match
from .antispam import confirmation_blocked, has_url
from .models import QuoteLine, QuoteSubmission
from .render import SPAM_SIGNALS, line_price_text, receipt_text

logger = logging.getLogger(__name__)

STAFF_SUBJECT = '❓ New Quote Request — {day}'                 # orders/services/notifications.py, 'quote'
THREAD_ID = '<quote-request-thread-{day}@dcmobilenotary.com>'
SEND_TRIES = 3
FAILURE_COUNT_TTL_S = 2 * 24 * 3600
OFFICE_PHONE = '+1 202-247-0837'

CONFIRMATION_SUBJECTS = {
    'en': 'Quote Request {reference} Received — DC Mobile Notary',
    'es': 'Solicitud {reference} recibida — DC Mobile Notary',
}
ES_REVIEW_PRICE = 'Precio tras una revisión gratuita'


# --- Staff email ------------------------------------------------------------------------------------

def thread_day(quote: QuoteSubmission) -> str:
    """The UTC day of the submission, as the legacy quote email dates its thread."""
    return (quote.created_at or timezone.now()).astimezone(dt_timezone.utc).strftime('%Y-%m-%d')


def staff_subject(quote: QuoteSubmission) -> str:
    return STAFF_SUBJECT.format(day=thread_day(quote))


def staff_headers(quote: QuoteSubmission) -> dict:
    """The legacy daily thread; Message-ID `<quote-{reference}@…>` cannot collide with `quote-request-{id}`."""
    thread = THREAD_ID.format(day=thread_day(quote))
    return {
        'Message-ID': f'<quote-{quote.reference}@dcmobilenotary.com>',
        'In-Reply-To': thread,
        'References': thread,
    }


def staff_recipients(quote: QuoteSubmission) -> list[str]:
    """ZZ TEST quotes go to QUOTES_TEST_NOTIFY_EMAILS only."""
    return flags.test_notify_emails() if quote.is_test else flags.notify_emails()


def staff_first_lines(quote: QuoteSubmission) -> list[str]:
    lines = []
    if quote.spam_status in ('suspect', 'released'):
        reasons = ', '.join(SPAM_SIGNALS.get(code, code.replace('_', ' ')) for code in quote.spam_reasons or [])
        lines.append(f'Possible spam ({reasons or "check"}) — nothing was created automatically. '
                     'If the request is real, use Create … or Add service…')
    lines.append(f'{quote.reference} · {quote.display_services} · {quote.full_name} (new multi-service form)')
    return lines


def staff_links(quote: QuoteSubmission) -> list[dict]:
    """Zoho record (or why it is missing), service records already made, decisions waiting, the admin."""
    links = []
    if quote.zoho_record_id:
        links.append({'label': 'Zoho quote:', 'url': quote.zoho_url})
    elif quote.sync_last_error:
        code = quote.sync_last_error.partition(': ')[2] or quote.sync_last_error
        links.append({'label': f'Zoho sync pending: {code} — see admin'})
    else:
        links.append({'label': 'Zoho: syncing — see admin'})
    phone_candidates = phone_match.candidate_lines(quote)
    phone_services = {service_key for service_key, _ in phone_candidates}
    for link in quote.service_links.order_by('created_at'):
        name = c.SERVICE_DISPLAY.get(link.service_key, link.service_key)
        if link.state in ('created', 'adopted') and link.target_url:
            note = '(created at intake)' if link.trigger == 'intake' and link.state == 'created' else f'({link.state})'
            links.append({'label': f'{name} record:', 'url': link.target_url, 'note': note})
        elif link.state == 'needs_decision' and link.service_key not in phone_services:
            links.append({'label': f'{name}: not created automatically — this client already has an open '
                                   'record. Link it or create new in Add service…'})
    for service_key in dict.fromkeys(service_key for service_key, _ in phone_candidates):
        name = c.SERVICE_DISPLAY.get(service_key, service_key)
        links.append({'label': f'Client also called — a phone-lead {name} record exists: '
                               'link it or create new in Add service…'})
    links.append({'label': 'Admin:', 'url': quote.admin_url})
    return links


def send_staff_email(quote: QuoteSubmission) -> None:
    """The office email; never for a held or spam quote (ops got an alert for the hold)."""
    if quote.sync_status in ('held', 'spam'):
        _set_status(quote, 'staff', 'skipped', only_from=('pending', 'failed'))
        return
    if not _claim(quote, 'staff'):
        return
    recipients = staff_recipients(quote)
    context = {'first_lines': staff_first_lines(quote), 'receipt': receipt_text(quote), 'links': staff_links(quote)}
    message = EmailMultiAlternatives(
        subject=staff_subject(quote),
        body=render_to_string('quotes/emails/staff.txt', context),
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@dcmobilenotary.net'),
        to=recipients,
        reply_to=[quote.email] if quote.email else None,
        headers=staff_headers(quote),
    )
    message.attach_alternative(render_to_string('quotes/emails/staff.html', context), 'text/html')
    _send(quote, 'staff', message)


# --- Client confirmation ------------------------------------------------------------------------------

def confirmation_allowed(quote: QuoteSubmission) -> bool:
    """Flag on, not held / spam / suspect, no URL in the name, not a ZZ TEST quote unless QUOTE_V3_TEST_EMAILS,
    and an address to send to."""
    return (
        flags.confirmation_email()
        and not confirmation_blocked(quote.spam_status, quote.spam_reasons)
        and quote.sync_status not in ('held', 'spam')
        and (not quote.is_test or flags.v3_test_emails())
        and bool(quote.email)
    )


def client_price(line: QuoteLine, locale: str) -> str:
    """The server's price string; an unapproved or review price reads "price after free review"."""
    text = line_price_text(line)
    if locale != 'es' or not text:
        return text
    if text == c.REVIEW_PRICE_TEXT:
        return ES_REVIEW_PRICE
    tier = (line.price_shown or {}).get('tier')
    approved = c.APPROVED_PRICES[(line.service_key, tier)]
    parts = [c.SERVICE_LABELS[line.service_key][1]]
    if tier:
        parts.append(c.PRICE_TIER_LABELS.get(tier, tier))
    per_document = f'+ ${approved.per_document_usd} por documento'
    parts += [f'desde ${approved.amount_usd}',
              f'{per_document} + tasa consular' if approved.plus_embassy_fee else per_document]
    return ' · '.join(parts)


def confirmation_context(quote: QuoteSubmission) -> dict:
    locale = 'es' if quote.locale == 'es' else 'en'
    index = 1 if locale == 'es' else 0
    services = [{'name': c.SERVICE_LABELS[line.service_key][index], 'price': client_price(line, locale)}
                for line in quote.lines.order_by('position')]
    review = ES_REVIEW_PRICE if locale == 'es' else c.REVIEW_PRICE_TEXT
    name = '' if has_url(quote.full_name) else quote.full_name   # the greeting never carries a link
    return {'name': name, 'reference': quote.reference, 'services': services, 'phone': OFFICE_PHONE,
            'phone_href': 'tel:+12022470837', 'reply_to': flags.reply_to(),
            'has_review': any(service['price'] == review for service in services)}


def send_confirmation(quote: QuoteSubmission) -> None:
    """At most once per quote: `pending → sending` is claimed first; Message-ID is deterministic."""
    if not confirmation_allowed(quote):
        _set_status(quote, 'confirmation', 'skipped' if flags.confirmation_email() else 'off', only_from=('pending',))
        return
    if not _claim(quote, 'confirmation'):
        return
    locale = 'es' if quote.locale == 'es' else 'en'
    context = confirmation_context(quote)
    message = EmailMultiAlternatives(
        subject=CONFIRMATION_SUBJECTS[locale].format(reference=quote.reference),
        body=render_to_string(f'quotes/emails/confirmation_{locale}.txt', context),
        from_email=settings.EMAIL_CLIENT_FROM,
        to=[quote.email],
        reply_to=[flags.reply_to()],
        headers={'Message-ID': f'<quote-{quote.reference}-confirm@dcmobilenotary.com>'},
    )
    message.attach_alternative(render_to_string(f'quotes/emails/confirmation_{locale}.html', context), 'text/html')
    _send(quote, 'confirmation', message)


# --- Shared ------------------------------------------------------------------------------------------

STATUS_FIELDS = {'staff': ('staff_email_status', 'staff_email_sent_at'),
                 'confirmation': ('confirmation_email_status', 'confirmation_email_sent_at')}


def rearm(quote: QuoteSubmission, kind: str) -> bool:
    """Admin "Resend": back to `pending`, never for a held or spam quote nor while a send is in flight."""
    if kind == 'confirmation' and not confirmation_allowed(quote):
        return False
    status_field, _ = STATUS_FIELDS[kind]
    updated = (QuoteSubmission.objects.filter(pk=quote.pk).exclude(sync_status__in=('held', 'spam'))
               .exclude(**{status_field: 'sending'}).update(**{status_field: 'pending'}, updated_at=timezone.now()))
    if updated:
        cache.delete(_failure_key(quote, kind))
        setattr(quote, status_field, 'pending')
    return bool(updated)


def _failure_key(quote: QuoteSubmission, kind: str) -> str:
    return f'quotes:email_failures:{quote.pk}:{kind}'


def _claim(quote: QuoteSubmission, kind: str) -> bool:
    status_field, _ = STATUS_FIELDS[kind]
    claimed = QuoteSubmission.objects.filter(pk=quote.pk, **{status_field: 'pending'}).update(
        **{status_field: 'sending'}, updated_at=timezone.now())
    if claimed:
        setattr(quote, status_field, 'sending')
    return bool(claimed)


def _set_status(quote: QuoteSubmission, kind: str, status: str, *, only_from: tuple[str, ...] = ('sending',),
                sent_at=None) -> None:
    status_field, sent_field = STATUS_FIELDS[kind]
    fields = {status_field: status}
    if sent_at is not None:
        fields[sent_field] = sent_at
    if QuoteSubmission.objects.filter(pk=quote.pk, **{f'{status_field}__in': only_from}).update(
            **fields, updated_at=timezone.now()):
        for name, value in fields.items():
            setattr(quote, name, value)


def _send(quote: QuoteSubmission, kind: str, message: EmailMultiAlternatives) -> None:
    if not message.to:
        logger.error('quote_email_no_recipient id=%s kind=%s', quote.pk, kind)
        _failed(quote, kind, final=True)
        return
    try:
        message.send()
    except Exception as exc:
        logger.warning('quote_email_send_failed id=%s kind=%s error=%s', quote.pk, kind, type(exc).__name__)
        if kind == 'confirmation' and outcome_unknown(exc):
            _unknown(quote, kind)   # the provider may have it: a retry could send the client a second one
        else:
            _failed(quote, kind)
        return
    _set_status(quote, kind, 'sent', sent_at=timezone.now())
    logger.info('quote_email_sent id=%s kind=%s', quote.pk, kind)


# Exception class names (any base) after which the provider may have the message. anymail raises an error that
# is also the requests exception; requests is not imported here (only crm/zoho.py makes HTTP calls, invariant 4).
UNKNOWN_OUTCOME_ERRORS = frozenset({'Timeout', 'ReadTimeout', 'ConnectionError', 'ChunkedEncodingError',
                                    'TimeoutError', 'ConnectionResetError', 'ConnectionAbortedError',
                                    'BrokenPipeError', 'SMTPServerDisconnected'})


def outcome_unknown(exc: BaseException) -> bool:
    """The request may have reached the mail provider: a read timeout or a connection dropped mid-request.
    A connect timeout never sent anything; an HTTP answer (a rejection) is definite and may be retried."""
    for error in (exc, exc.__cause__, exc.__context__):
        if error is None:
            continue
        names = {cls.__name__ for cls in type(error).__mro__}
        if 'ConnectTimeout' in names:
            return False
        if names & UNKNOWN_OUTCOME_ERRORS:
            return True
    return False


def _unknown(quote: QuoteSubmission, kind: str) -> None:
    """Never retried automatically: `failed`, and ops checks the provider log before using Resend."""
    _set_status(quote, kind, 'failed')
    alert_ops(
        'quote_email_unknown', f'{quote.reference}:{kind}',
        f'Quote {quote.reference}: {kind} email may have been sent',
        f'Sending the {kind} email of {quote.reference} timed out or lost the connection after the request, so the '
        'client may have received it. It was not retried automatically. Check the Resend log first; use "Resend" '
        f'in the admin only if it was not delivered.\n{quote.admin_url}',
    )


def _failed(quote: QuoteSubmission, kind: str, *, final: bool = False) -> None:
    """Back to `pending` for the reconciler; the third failure marks it `failed` and alerts ops."""
    key = _failure_key(quote, kind)
    cache.add(key, 0, FAILURE_COUNT_TTL_S)
    if not final and cache.incr(key) < SEND_TRIES:
        _set_status(quote, kind, 'pending')
        return
    _set_status(quote, kind, 'failed')
    alert_ops(
        'quote_email_failed', f'{quote.reference}:{kind}',
        f'Quote {quote.reference}: {kind} email not sent',
        f'The {kind} email of {quote.reference} failed {SEND_TRIES} times (or has no recipient). '
        f'Use "Resend" in the admin once mail works again.\n{quote.admin_url}',
    )
