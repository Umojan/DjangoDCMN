"""EN texts of a submission for staff and Zoho (DESIGN §5.4, §5.5): line summaries, prices, receipts.

`comments_text` → `Client_Comments` (≤2000), `receipt_text` → the receipt Note and the staff email (≤10,000),
`lines_text` → `Quote_Lines_Text` (≤2000; re-rendered by the engine write-back with the ServiceLink states).
Prices are always the server's (`price_text`); the client's `price_shown.text` only appears as
`client saw: "…" (unapproved)` when it differs.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.utils import timezone

from . import contract as c
from .models import QuoteLine, QuoteSubmission

if TYPE_CHECKING:
    from crm.models import ServiceLink

ET = ZoneInfo('America/New_York')
COMMENTS_MAX, RECEIPT_MAX, LINES_MAX = 2000, 10000, 2000
CLIENT_TEXT_MAX = 200
QUOTED_TEXT_MAX = 40
MORE_IN_NOTES = '… (full request in Notes)'
LINES_MORE_IN_NOTES = '… (full list in Notes)'

REVIEW_CHECKS = {
    'same_language': 'languages identical – confirm',
    'will_trust_online': 'will or trust online – confirm it can be done by RON',
    'route_pending': 'apostille or embassy route not confirmed by the client',
}
SPAM_SIGNALS = {
    'message_urls': 'URLs in message',
    'url_in_name': 'URL in name',
    'turnstile_missing': 'no Turnstile',
    'turnstile_error': 'Turnstile unavailable',
    'turnstile_failed': 'Turnstile failed',
    'disposable_email': 'disposable email',
    'no_timing': 'no timing',
}
LINK_STATUS = {
    'suggested': 'SUGGESTED',
    'dismissed': 'DISMISSED',
    'pending': 'CREATING',
    'creating': 'CREATING',
    'retry': 'CREATING',
    'needs_decision': 'NEEDS DECISION',
    'degraded': 'SETUP CHANGED',
    'failed': 'FAILED',
    'target_deleted': 'REQUESTED (Zoho record deleted)',
}
NO_PIPELINE = 'NO PIPELINE (handle from the quote)'


# --- Prices (DESIGN §5.4) ------------------------------------------------------------------------

def price_text(service: str, tier: str | None, flag: str) -> str:
    """The server's price string, worded as the v5 card: `Apostille · Federal · from $125 · + $25 per document`.

    Only an approved price is rendered with amounts; review, unapproved and route-pending lines read
    `Price after free review` (what the client email says for them); a missing price is ''.
    """
    if flag == 'missing':
        return ''
    approved = c.APPROVED_PRICES.get((service, tier))
    if flag != 'approved' or approved is None:
        return c.REVIEW_PRICE_TEXT
    parts = [c.SERVICE_LABELS[service][0]]
    if tier:
        parts.append(c.PRICE_TIER_LABELS.get(tier, tier))
    per_document = f'+ ${approved.per_document_usd} per document'
    parts += [f'from ${approved.amount_usd}', f'{per_document} + embassy fee' if approved.plus_embassy_fee
              else per_document]
    return ' · '.join(parts)


def short_price(service: str, tier: str | None, flag: str) -> str:
    """The `Quote_Lines_Text` form: `from $125 + $25/doc`, `price after free review`, or ''."""
    if flag == 'missing':
        return ''
    approved = c.APPROVED_PRICES.get((service, tier))
    if flag != 'approved' or approved is None:
        return 'price after free review'
    text = f'from ${approved.amount_usd} + ${approved.per_document_usd}/doc'
    return f'{text} + embassy fee' if approved.plus_embassy_fee else text


def line_price_text(line: QuoteLine) -> str:
    return price_text(line.service_key, _tier(line), line.price_flag)


def client_saw(line: QuoteLine) -> str:
    """`client saw: "<text>" (unapproved)` when the client's price text differs from the server's, else ''."""
    shown = ' '.join((line.price_shown_text or '').split())
    if not shown or shown.casefold() == line_price_text(line).casefold():
        return ''
    return f'client saw: "{shown[:CLIENT_TEXT_MAX]}" (unapproved)'


def _tier(line: QuoteLine) -> str | None:
    tier = (line.price_shown or {}).get('tier')
    return tier if isinstance(tier, str) else None


# --- Lines ---------------------------------------------------------------------------------------

def line_summary(line: QuoteLine) -> str:
    """`Translation (add-on of Apostille) — English → Spanish · Traducción jurada` (no number, no price)."""
    name = c.SERVICE_DISPLAY.get(line.service_key, line.service_key)
    if line.addon_of:
        name += f' (add-on of {c.SERVICE_DISPLAY.get(line.addon_of, line.addon_of)})'
    elif line.switched_from:
        name += f' (switched from {c.SERVICE_DISPLAY.get(line.switched_from, line.switched_from)})'
    parts = _answer_parts(line.service_key, line.answers or {})
    return f'{name} — {" · ".join(parts)}' if parts else name


def _answer_parts(service: str, answers: dict) -> list[str]:
    def label(key: str) -> str:
        value = answers.get(key)
        return c.ANSWER_LABELS.get((service, key), {}).get(value, str(value)) if value is not None else ''

    def count(key: str, one: str, many: str) -> str:
        value = answers.get(key)
        return '' if value is None else f'{value} {one if value == 1 else many}'

    def route(doc: str) -> str:
        country = c.country_name(answers['country']) if answers.get('country') else ''
        return ' → '.join(part for part in (doc, country) if part) if doc else (f'→ {country}' if country else '')

    if service == 'apostille':
        state = answers.get('state')
        parts = [
            route(label('doc')),
            f'State: {"not sure" if state == c.STATE_UNSURE else c.US_STATES.get(state, state)}' if state else '',
            f'FBI PDF: {label("fbi_pdf").lower()}' if answers.get('fbi_pdf') else '',
            count('count', 'document', 'documents'),
        ]
    elif service == 'embassy':
        kind = label('kind')
        parts = [
            route(f'{kind} documents' if kind and answers.get('kind') != 'unsure' else kind),
            f'document: {label("doc_from_apostille")}' if answers.get('doc_from_apostille') else '',
            count('count', 'document', 'documents'),
        ]
    elif service == 'translation':
        source, target = (c.language_name(answers[k]) if answers.get(k) else '' for k in ('from', 'to'))
        parts = [
            f'{source or "?"} → {target or "?"}' if source or target else '',
            label('kind'),
            count('pages', 'page', 'pages'),
        ]
    elif service == 'fbi':
        parts = [label('where'), _needs_apostille(label('apostille')), count('people', 'person', 'people')]
    elif service == 'ron':
        parts = [label('doc'), _needs_apostille(label('apostille'))]
    elif service == 'fingerprinting':
        parts = [label('type'), label('where'), count('people', 'person', 'people')]
    elif service == 'mobile-notary':
        parts = [label('doc'), f'ZIP {answers["zip"]}' if answers.get('zip') else '', label('when')]
    elif service == 'office-notary':
        parts = [label('doc'), label('office')]
    elif service in ('retrieval', 'other'):
        text = answers.get('record' if service == 'retrieval' else 'text') or ''
        parts = [_quoted(text)]
    else:
        parts = []
    return [part for part in parts if part]


def _needs_apostille(answer: str) -> str:
    return f'apostille needed: {answer.lower()}' if answer else ''


def _quoted(text: str) -> str:
    if not text:
        return ''
    flat = ' '.join(text.split())
    return f'"{flat[:QUOTED_TEXT_MAX]}…"' if len(flat) > QUOTED_TEXT_MAX else f'"{flat}"'


def _lines(quote: QuoteSubmission) -> list[QuoteLine]:
    return list(quote.lines.order_by('position'))


def _suggested(lines: Sequence[QuoteLine]) -> list[tuple[str, str]]:
    """(implied service, reason) for every implied service that is not already a line."""
    keys = {line.service_key for line in lines}
    found: dict[str, str] = {}
    for line in lines:
        for implied in line.implied_services or []:
            if implied in keys or implied in found:
                continue
            found[implied] = next((reason for (service, _key), (target, reason) in c.IMPLIED_BY.items()
                                   if service == line.service_key and target == implied), '')
    return list(found.items())


def _suggested_text(service: str, reason: str) -> str:
    name = c.SERVICE_DISPLAY.get(service, service)
    return f'Suggested: {name} ({reason})' if reason else f'Suggested: {name}'


# --- Shared blocks -------------------------------------------------------------------------------

def _header(quote: QuoteSubmission, lines: Sequence[QuoteLine]) -> str:
    count = len(lines)
    return (f'[{quote.schema_version}] {quote.reference} · {count} service{"" if count == 1 else "s"} · '
            f'{quote.locale.upper()} · web form {quote.form_version}')


def _checks(quote: QuoteSubmission) -> list[str]:
    """The ⚠ / Check lines under the header (repeat, spam)."""
    rows = []
    earlier = quote.possible_repeat_of
    if earlier is not None:
        rows.append(f'⚠ Possible repeat of {earlier.reference} (same email, '
                    f'{_ago(earlier.created_at, quote.created_at)})')
    if quote.spam_status == 'suspect':
        signals = ', '.join(SPAM_SIGNALS.get(code, code) for code in quote.spam_reasons or [])
        rows.append(f'Check: possible spam ({signals})' if signals else 'Check: possible spam')
    return rows


def _ago(earlier: datetime | None, later: datetime | None) -> str:
    if earlier is None:
        return 'earlier'
    seconds = max(((later or timezone.now()) - earlier).total_seconds(), 0)
    if seconds < 3600:
        return f'{max(int(seconds // 60), 1)} min ago'
    return f'{round(seconds / 3600)} h ago'


def _line_block(line: QuoteLine) -> list[str]:
    rows = [f'{line.position}. {line_summary(line)}']
    price = line_price_text(line)
    rows.append(f'   Price shown: {price}' if price else '   Price shown: none')
    saw = client_saw(line)
    if saw:
        rows.append(f'   {saw}')
    rows += [f'   Check: {REVIEW_CHECKS.get(reason, reason)}' for reason in line.review_reasons or []]
    return rows


def contact_line(quote: QuoteSubmission) -> str:
    """`Contact: WhatsApp (opted in) · When: As soon as possible`; a channel without its opt-in is explained."""
    preferred, effective = quote.preferred_channel, quote.effective_channel
    if preferred and preferred != effective:
        contact = f'Contact: asked for {c.CHANNEL_DISPLAY.get(preferred, preferred)}, no opt-in — use call or email'
    elif effective in ('whatsapp', 'text'):
        contact = f'Contact: {c.CHANNEL_DISPLAY[effective]} (opted in)'
    elif effective:
        contact = f'Contact: {c.CHANNEL_DISPLAY.get(effective, effective)}'
    else:
        contact = 'Contact: no preference'
    when = _when(quote)
    return f'{contact} · When: {when}' if when else contact


def _when(quote: QuoteSubmission) -> str:
    if quote.deadline:
        return f'By {quote.deadline.isoformat()}'
    return c.WHEN_LABELS.get(quote.when, '')


def source_page(quote: QuoteSubmission) -> str:
    """`/schedule-an-appointment?service=apostille&doc=fbi ← /apostille-fbi`."""
    parts = urlsplit(quote.page_url) if quote.page_url else None
    page = (parts.path + (f'?{parts.query}' if parts.query else '')) if parts and parts.path else quote.page_path
    if quote.referrer_path:
        return f'{page} ← {quote.referrer_path}' if page else f'← {quote.referrer_path}'
    return page


# --- Client_Comments (≤2000) -------------------------------------------------------------------------

def comments_text(quote: QuoteSubmission, limit: int = COMMENTS_MAX) -> str:
    """DESIGN §5.5 block. Over the limit it keeps line 1 and the services, cuts the message, marks the cut."""
    lines = _lines(quote)
    head = [_header(quote, lines), *_checks(quote)]
    for line in lines:
        head += _line_block(line)
    head += [_suggested_text(service, reason) for service, reason in _suggested(lines)]
    contact = [contact_line(quote)]
    page = source_page(quote)
    tail = [f'From: {page}'] if page else []
    message = ' '.join(quote.message.split())

    full = '\n'.join(head + contact + ([f'Message: {message}'] if message else []) + tail)
    if len(full) <= limit:
        return full

    fixed = '\n'.join(head + contact + tail + [MORE_IN_NOTES])
    room = limit - len(fixed) - len('\nMessage: …')
    if message and room > 0:
        fixed = '\n'.join(head + contact + [f'Message: {message[:room]}…'] + tail + [MORE_IN_NOTES])
    if len(fixed) <= limit:
        return fixed
    return f'{fixed[:limit - len(MORE_IN_NOTES) - 1]}\n{MORE_IN_NOTES}'


# --- Receipt Note and staff email (≤10,000) -------------------------------------------------------------

def receipt_text(quote: QuoteSubmission, limit: int = RECEIPT_MAX, *, contact_notes: Sequence[str] = ()) -> str:
    """The full receipt: every answer (raw value when it differs), dropped answers, entry, attribution.

    `contact_notes` are the Zoho Contact differences, known only to the receipt Note step.
    """
    lines = _lines(quote)
    rows = [_header(quote, lines), f'Received: {_et(quote.created_at)}', *_checks(quote)]
    rows += _price_warnings(lines)
    if quote.needs_review:
        rows.append('Check: needs review (see the lines below)')
    rows += ['', 'Client', f'Name: {quote.full_name}', f'Email: {quote.email}']
    if len(quote.email) > 100:
        rows.append('Check: email longer than 100 characters, not written to the Zoho email fields')
    rows.append(f'Phone: {quote.phone_e164 or quote.phone_raw}'
                + ('' if quote.phone_valid else ' (not recognised as a valid number, as typed)'))
    rows += [contact_line(quote), _consent_line(quote), *contact_notes]

    rows += ['', 'Services']
    for line in lines:
        rows += _line_block(line)
        rows += _answer_rows(line)
    rows += [_suggested_text(service, reason) for service, reason in _suggested(lines)]
    if quote.price_notes:
        rows.append(f'Price notes: {", ".join(quote.price_notes)}')

    if quote.message:
        rows += ['', 'Message', quote.message]
    rows += ['', *_context_rows(quote)]
    rows += _amendment_rows(quote)
    if quote.files_dropped:
        rows.append('Files: the client tried to attach files; uploads are off, ask for them by email.')
    if quote.contract_warnings:
        rows.append(f'Contract warnings: {", ".join(quote.contract_warnings)}')

    text = '\n'.join(rows).strip()
    if len(text) <= limit:
        return text
    return f'{text[:limit - len(MORE_IN_NOTES) - 1]}\n{MORE_IN_NOTES}'


def _et(moment: datetime | None) -> str:
    return (moment or timezone.now()).astimezone(ET).strftime('%Y-%m-%d %H:%M ET')


def _price_warnings(lines: Sequence[QuoteLine]) -> list[str]:
    flagged = [str(line.position) for line in lines if line.price_flag == 'unapproved']
    if not flagged:
        return []
    return [f'Check: unapproved price on line {", ".join(flagged)} (price table {c.PRICE_TABLE_VERSION}); '
            'quote it after review']


def _consent_line(quote: QuoteSubmission) -> str:
    parts = [f'terms {quote.terms_method}' + (f' {quote.terms_version}' if quote.terms_version else '')]
    if quote.consent_text_version:
        parts.append(quote.consent_text_version)
    parts += [f'WhatsApp opt-in {"yes" if quote.whatsapp_opt_in else "no"}',
              f'SMS opt-in {"yes" if quote.sms_opt_in else "no"}']
    return f'Consent: {" · ".join(parts)}'


def _answer_rows(line: QuoteLine) -> list[str]:
    answers, raw = line.answers or {}, line.answers_raw or {}
    rows = []
    if answers:
        shown = []
        for key, value in answers.items():
            original = raw.get(key)
            text = f'{key}={value}'
            if original is not None and original != value:
                text += f' (sent "{_short(original)}")'
            shown.append(text)
        rows.append(f'   Answers: {", ".join(shown)}')
    if line.dropped_answers:
        dropped = [f'{key}="{_short(raw.get(key))}" ({reason})' for key, reason in line.dropped_answers.items()]
        rows.append(f'   Dropped: {", ".join(dropped)}')
    origin = [f'source {line.source}'] if line.source else []
    if line.addons:
        origin.append(f'add-ons {", ".join(line.addons)}')
    if line.route:
        route = line.route
        origin.append(f'route {route.get("expected")} for {route.get("country")} ({route.get("status")})')
    if origin:
        rows.append(f'   {" · ".join(origin)}')
    return rows


def _short(value, limit: int = 100) -> str:
    text = '' if value is None else ' '.join(str(value).split())
    return f'{text[:limit]}…' if len(text) > limit else text


def _context_rows(quote: QuoteSubmission) -> list[str]:
    rows = []
    entry = quote.entry or {}
    if entry.get('type'):
        params = ' · '.join(f'{key}={value}' for key, value in (entry.get('params') or {}).items() if value)
        preselected = ', '.join(entry.get('preselected') or [])
        rows.append(' · '.join(part for part in (
            f'Entry: {entry["type"]}', params, f'preselected {preselected}' if preselected else '',
            f'CTA {entry["cta_id"]}' if entry.get('cta_id') else '',
        ) if part))
    page = source_page(quote)
    if page:
        rows.append(f'From: {page}')
    attribution = attribution_line(quote.attribution_data)
    if attribution:
        rows.append(f'Attribution: {attribution}')
    return rows


def attribution_line(data: dict | None) -> str:
    """`google / organic · first visit 25 Nov · 5 pages`."""
    if not data:
        return ''
    source = data.get('source') or data.get('utm_source')
    medium = data.get('medium') or data.get('utm_medium')
    parts = [' / '.join(str(v) for v in (source, medium) if v)]
    if data.get('campaign'):
        parts.append(f'campaign {data["campaign"]}')
    first = _parse_iso(data.get('first_visit_at'))
    if first:
        parts.append(f'first visit {first.astimezone(ET).day} {first.astimezone(ET):%b}')
    if data.get('pages_viewed'):
        parts.append(f'{data["pages_viewed"]} pages')
    if data.get('gclid'):
        parts.append('gclid')
    return ' · '.join(part for part in parts if part)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ET)


def _amendment_rows(quote: QuoteSubmission) -> list[str]:
    rows = []
    for number, amendment in enumerate(quote.amendments or [], start=1):
        changed = ', '.join(amendment.get('changed') or []) or 'no visible field'
        at = _parse_iso(amendment.get('at'))
        rows.append(f'Amendment {number} ({_et(at) if at else "?"}): client re-sent with changes: {changed}')
    return rows


# --- Quote_Lines_Text (≤2000) ----------------------------------------------------------------------

def lines_text(quote: QuoteSubmission, links: Mapping[str, 'ServiceLink'] | None = None,
               limit: int = LINES_MAX) -> str:
    """One row per line with its status in words; `links` maps service_key → ServiceLink (none at create)."""
    lines = _lines(quote)
    rows = []
    for line in lines:
        price = short_price(line.service_key, _tier(line), line.price_flag)
        status = line_status(line.service_key, (links or {}).get(line.service_key))
        rows.append((f'{line.position}. ', line_summary(line), (f' · {price}' if price else '') + f' — {status}'))
    for service, reason in _suggested(lines):
        link = (links or {}).get(service)
        status = '' if link is None or link.state == 'suggested' else f' — {line_status(service, link)}'
        rows.append(('', _suggested_text(service, reason), status))

    text = '\n'.join(''.join(row) for row in rows)
    if len(text) <= limit:
        return text
    # Too long: shorten every summary evenly so each row keeps its number and status.
    fixed = sum(len(prefix) + len(suffix) + 1 for prefix, _summary, suffix in rows) + len(LINES_MORE_IN_NOTES)
    room = max((limit - fixed) // max(len(rows), 1) - 1, 0)
    shortened = [prefix + (summary if len(summary) <= room else f'{summary[:room]}…') + suffix
                 for prefix, summary, suffix in rows]
    text = '\n'.join(shortened + [LINES_MORE_IN_NOTES])
    return text if len(text) <= limit else f'{text[:limit - len(LINES_MORE_IN_NOTES) - 1]}\n{LINES_MORE_IN_NOTES}'


def line_status(service_key: str, link: 'ServiceLink | None') -> str:
    """REQUESTED, SUGGESTED, CREATING, CREATED → <module> (<date>, <trigger>), LINKED → <module>, …"""
    if link is None:
        return NO_PIPELINE if service_key in c.NO_TARGET_SERVICES else 'REQUESTED'
    if link.state == 'created':
        when = _day(link.done_at or link.updated_at)
        return f'CREATED → {_module(link)} ({when}, {link.trigger})' if link.trigger else \
            f'CREATED → {_module(link)} ({when})'
    if link.state == 'adopted':
        return f'LINKED → {_module(link)}'
    return LINK_STATUS.get(link.state, link.state.upper())


def _module(link: 'ServiceLink') -> str:
    return (link.target_module or 'Zoho').replace('_', ' ')


def _day(moment: datetime | None) -> str:
    local = (moment or timezone.now()).astimezone(ET)
    return f'{local.day} {local:%b}'
