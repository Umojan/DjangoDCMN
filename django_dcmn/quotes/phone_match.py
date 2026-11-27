"""Phone leads of the same client as a v3 quote (DESIGN §5.7, option b of QUESTIONS V21).

A call before the quote is matched by step 5 of the runner (`match_before`); a call after it by the
10-minute beat (`link_recent`). Each match is a `QuotePhoneMatch` row plus Notes both ways. When the phone
record sits at `Phone Call Received` in the target module of a requested line, the match is a `candidate`
that intake and the engine's adoption offer as "Link this record" (never auto-adopted).
`PhoneCallLead` is read only: `matched_*` is never written (invariant 11).
"""

import logging
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.utils import timezone

from crm import zoho
from orders.models import PhoneCallLead
from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

from .models import GAQL_MODULE, GAQL_TAB, QuotePhoneMatch, QuoteSubmission
from .render import ET

logger = logging.getLogger(__name__)

BEFORE_WINDOW = timedelta(days=14)
QUOTE_WINDOW = timedelta(days=30)
PHONE_CALL_RECEIVED = 'Phone Call Received'
DONE_LINK_STATES = ('created', 'adopted')
LEAD_FIELDS = ('pk', 'whatconverts_lead_id', 'contact_phone', 'contact_email', 'zoho_module', 'zoho_lead_id',
               'call_duration', 'landing_url', 'created_at')
QUOTE_FIELDS = ('pk', 'reference', 'created_at', 'phone_last10', 'email_normalized', 'zoho_record_id')

# Modules WhatConverts creates phone leads in (orders/services/whatconverts.py), with tab and stage field.
PHONE_LEAD_MODULES = {
    GAQL_MODULE: (GAQL_TAB, 'GET_A_QUOTE_LEADS'),
    'Apostille_Services': ('CustomModule2', 'Status'),
    'Embassy_Legalization': ('CustomModule1', 'Status'),
    'Translation_Services': ('CustomModule3', 'Translation_Status'),
    'Notary_Services': ('CustomModule6', 'Notary_Stages'),
    'I_9_Verification': ('CustomModule5', 'Stage'),
    'Triple_Seal_Apostilles': ('CustomModule4', 'Stage'),
    'Deals': ('Potentials', 'Stage'),
}
# Target module of each quote line (DESIGN §7.4); `retrieval` and `other` have none.
SERVICE_TARGET_MODULES = {
    'apostille': 'Apostille_Services',
    'embassy': 'Embassy_Legalization',
    'translation': 'Translation_Services',
    'fbi': 'FINGERPRINT_SERVICES',
    'fingerprinting': 'FINGERPRINT_SERVICES',
    'mobile-notary': 'Notary_Services',
    'office-notary': 'Notary_Services',
    'ron': 'Notary_Services',
    'i9': 'I_9_Verification',
}


def last10(phone: str | None) -> str:
    digits = re.sub(r'\D', '', phone or '')
    return digits[-10:] if len(digits) >= 10 else ''


def match_on(quote: QuoteSubmission, lead: PhoneCallLead) -> str:
    """'phone' or 'email' when the call is of the same client, else ''."""
    if quote.phone_last10 and last10(lead.contact_phone) == quote.phone_last10:
        return 'phone'
    if quote.email_normalized and (lead.contact_email or '').strip().casefold() == quote.email_normalized:
        return 'email'
    return ''


def lead_module(lead: PhoneCallLead) -> str:
    return lead.zoho_module or GAQL_MODULE      # sync_phone_lead_to_zoho's default


def lead_url(lead: PhoneCallLead) -> str:
    tab = PHONE_LEAD_MODULES.get(lead_module(lead), ('', ''))[0]
    return zoho.record_url(tab, lead.zoho_lead_id) if tab else ''


# --- Call before the quote (runner step 5) ---------------------------------------------------------------

def match_before(quote: QuoteSubmission) -> list[QuotePhoneMatch]:
    """Phone leads of the last 14 days before the quote, by last-10 phone digits or email."""
    created = quote.created_at or timezone.now()
    leads = (PhoneCallLead.objects.filter(created_at__gte=created - BEFORE_WINDOW, created_at__lte=created)
             .exclude(zoho_lead_id='').only(*LEAD_FIELDS).order_by('created_at'))
    return [_record(quote, lead, on, 'call_before_quote') for lead in leads if (on := match_on(quote, lead))]


# --- Call after the quote (beat, every 10 min) ----------------------------------------------------------

def link_recent(hours: int = 24) -> int:
    """Link phone leads of the last `hours` to the latest earlier v3 quote of the same client (≤30 days)."""
    now = timezone.now()
    leads = (PhoneCallLead.objects.filter(created_at__gte=now - timedelta(hours=hours))
             .exclude(zoho_lead_id='').only(*LEAD_FIELDS).order_by('created_at'))
    quotes = list(QuoteSubmission.objects.filter(created_at__gte=now - QUOTE_WINDOW)
                  .exclude(zoho_record_id='').exclude(sync_status__in=('held', 'spam'))
                  .exclude(spam_status__in=('held', 'spam')).only(*QUOTE_FIELDS).order_by('-created_at'))
    linked = 0
    for lead in leads:
        if QuotePhoneMatch.objects.filter(phone_lead_id=lead.pk).exclude(action='').exists():
            continue
        quote = next((q for q in quotes if q.created_at < lead.created_at and match_on(q, lead)), None)
        if quote is None:
            continue
        try:
            _record(quote, lead, match_on(quote, lead), 'call_after_quote')
        except (ZohoTransientError, ZohoPermanentError) as exc:   # the next beat resumes this lead
            logger.warning('quote_phone_link_failed id=%s lead=%s code=%s', quote.pk, lead.pk,
                           getattr(exc, 'code', '') or type(exc).__name__)
            continue
        linked += 1
    return linked


def followup_calls_on(day: date) -> int:
    """Digest line `followup_call_record`: follow-up calls of that ET day whose phone record was created."""
    start = datetime.combine(day, time.min, tzinfo=ET)
    return QuotePhoneMatch.objects.filter(
        direction='call_after_quote', created_at__gte=start, created_at__lt=start + timedelta(days=1),
    ).exclude(phone_record_id='').count()


# --- Shared ---------------------------------------------------------------------------------------------

def _record(quote: QuoteSubmission, lead: PhoneCallLead, on: str, direction: str) -> QuotePhoneMatch:
    """Store the match, post the two Notes (once, by title prefix), then decide noted / candidate."""
    match, _ = QuotePhoneMatch.objects.get_or_create(quote=quote, phone_lead=lead, defaults={
        'whatconverts_lead_id': lead.whatconverts_lead_id, 'direction': direction, 'match_on': on,
        'phone_record_module': lead_module(lead), 'phone_record_id': lead.zoho_lead_id,
    })
    if match.action:      # complete; a row without action is resumed (its Notes are found by title)
        return match
    match.note_ids = _post_notes(quote, lead, match)
    match.action = 'candidate' if _is_candidate(quote, match) else 'noted'
    match.save(update_fields=['note_ids', 'action'])
    logger.info('quote_phone_match id=%s lead=%s direction=%s action=%s', quote.pk, lead.pk, direction, match.action)
    return match


def _post_notes(quote: QuoteSubmission, lead: PhoneCallLead, match: QuotePhoneMatch) -> list[str]:
    after = match.direction == 'call_after_quote'
    phone_url = lead_url(lead)
    quote_title = f'{quote.reference} · {"follow-up" if after else "earlier"} call {lead.whatconverts_lead_id}'
    quote_text = (f'Client called {"again " if after else ""}on {_day(lead.created_at)}{_call_details(lead, after)}. '
                  f'Phone lead: {phone_url or lead_module(lead)}')
    phone_title = f'{"Follow-up call to web quote" if after else "Web quote"} {quote.reference}'
    phone_text = (f'Follow-up call to web quote {quote.reference}: {quote.zoho_url}. '
                  'Probably not a new request — check before working it' if after
                  else f'Web quote {quote.reference} has the details: {quote.zoho_url}')
    note_ids = []
    if quote.zoho_record_id:
        note_ids.append(_note_once(GAQL_MODULE, quote.zoho_record_id, quote_title, quote_text))
    if lead_module(lead) in PHONE_LEAD_MODULES:
        note_ids.append(_note_once(lead_module(lead), lead.zoho_lead_id, phone_title, phone_text))
    return [note_id for note_id in note_ids if note_id]


def _note_once(module: str, record_id: str, title: str, content: str) -> str:
    notes = zoho.list_notes(module, record_id)
    existing = next((str(n['id']) for n in notes if str(n.get('Note_Title') or '').startswith(title)), '')
    return existing or zoho.add_note(module, record_id, title, content)


def _is_candidate(quote: QuoteSubmission, match: QuotePhoneMatch) -> bool:
    """The phone record is in the target module of a requested line not yet created, still at Phone Call Received."""
    module = match.phone_record_module
    services = {line.service_key for line in quote.lines.all()
                if SERVICE_TARGET_MODULES.get(line.service_key) == module}
    if not services or module not in PHONE_LEAD_MODULES:
        return False
    done = set(quote.service_links.filter(state__in=DONE_LINK_STATES).values_list('service_key', flat=True))
    if services <= done:
        return False
    stage_field = PHONE_LEAD_MODULES[module][1]
    record = zoho.get(module, match.phone_record_id, fields=[stage_field])
    return bool(record) and record.get(stage_field) == PHONE_CALL_RECEIVED


def candidate_lines(quote: QuoteSubmission) -> list[tuple[str, QuotePhoneMatch]]:
    """(service_key, match) for each requested line with a phone-lead candidate; for the staff email."""
    lines = list(quote.lines.order_by('position'))
    found = []
    for match in quote.phone_matches.filter(action='candidate').order_by('created_at'):
        for line in lines:
            if SERVICE_TARGET_MODULES.get(line.service_key) == match.phone_record_module:
                found.append((line.service_key, match))
    return found


def _day(moment: datetime | None) -> str:
    local = (moment or timezone.now()).astimezone(ET)
    return f'{local.day} {local:%b}'


def _call_details(lead: PhoneCallLead, after: bool) -> str:
    parts = []
    if lead.call_duration:
        parts.append(f'{max(1, round(lead.call_duration / 60))} min')
    if not after and lead.landing_url:
        parts.append(f'landing {urlparse(lead.landing_url).path or "/"}')
    return f' ({", ".join(parts)})' if parts else ''

