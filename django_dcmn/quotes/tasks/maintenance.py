"""Phone-lead linking, PII purge and erase (DESIGN §4.5, §5.7)."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.models import EngineRequest, ServiceLink, ServiceSignal
from orders.models import Track

from .. import contract as c
from .. import flags, phone_match
from ..models import QuoteLine, QuoteRejection, QuoteSubmission
from ..render import line_summary

logger = logging.getLogger(__name__)

IP_UA_DAYS = 90
RAW_PAYLOAD_DAYS = 365
REJECTION_DAYS = 30


@shared_task(name='quotes.tasks.link_recent_phone_leads')
def link_recent_phone_leads() -> str:
    """Beat, every 600 s: link follow-up calls to v3 quotes (DESIGN §5.7 option b)."""
    if not (flags.quote_v3_enabled() and flags.zoho_sync()):
        return 'skipped'
    return f'linked={phone_match.link_recent()}'


@shared_task(name='quotes.tasks.purge_quote_pii')
def purge_quote_pii() -> str:
    """Beat, daily 03:30 UTC: retention windows of DESIGN §4.5 (QUESTIONS V8)."""
    counts = purge()
    return ' '.join(f'{name}={count}' for name, count in counts.items())


def purge(now=None) -> dict[str, int]:
    now = now or timezone.now()
    ip_ua = QuoteSubmission.objects.filter(
        Q(ip__isnull=False) | ~Q(user_agent=''), created_at__lt=now - timedelta(days=IP_UA_DAYS),
    ).update(ip=None, user_agent='', updated_at=now)
    old = QuoteSubmission.objects.filter(created_at__lt=now - timedelta(days=RAW_PAYLOAD_DAYS))
    raw = old.filter(raw_payload__isnull=False).update(raw_payload=None, updated_at=now)
    for quote in old.exclude(amendments=[]).only('pk', 'amendments'):
        if any(entry.get('payload') for entry in quote.amendments):
            QuoteSubmission.objects.filter(pk=quote.pk).update(
                amendments=_without_payloads(quote.amendments), updated_at=now)
    rejections, _ = QuoteRejection.objects.filter(created_at__lt=now - timedelta(days=REJECTION_DAYS)).delete()
    logger.info('quote_pii_purged ip_ua=%s raw=%s rejections=%s', ip_ua, raw, rejections)
    return {'ip_ua': ip_ua, 'raw_payload': raw, 'rejections': rejections}


# --- Erase request (admin "Erase PII", `manage.py quotes_erase`) -------------------------------------------

def erase_quote_pii(quote: QuoteSubmission) -> None:
    """Blank the contact columns, raw payloads, message, page URL, attribution, the lines' free-text answers and
    summaries, and the name/email of the quote's silent tracking pages; `reference` and the line keys stay."""
    now = timezone.now()
    with transaction.atomic():
        QuoteSubmission.objects.filter(pk=quote.pk).update(
            full_name='', email='', email_normalized='', phone_raw='', phone_e164='', phone_last10='',
            message='', raw_payload=None, amendments=_without_payloads(quote.amendments or []), ip=None,
            user_agent='', page_url='', attribution_data=None, pii_erased_at=now, updated_at=now,
        )
        for line in QuoteLine.objects.filter(quote=quote):
            _erase_line(line)
        _erase_tracks(quote)
        _erase_crm_rows(
            Q(source_key=f'qr:{quote.pk}') | Q(quote=quote) | _record_q(quote.zoho_record_id),
            Q(source_key=f'qr:{quote.pk}') | _record_q(quote.zoho_record_id),
            Q(source_key=f'qr:{quote.pk}') | Q(quote=quote) | _record_q(quote.zoho_record_id),
        )
    quote.refresh_from_db()
    logger.info('quote_pii_erased id=%s', quote.pk)


def _erase_line(line: QuoteLine) -> None:
    """Free-text answers (retrieval `record`, other `text`) are the client's own words, like `message`."""
    schema = c.ANSWERS.get(line.service_key, {})
    line.answers = {key: ('' if key in schema and schema[key].kind == c.TEXT else value)
                    for key, value in (line.answers or {}).items()}
    line.answers_raw = {}
    line.summary_server = line_summary(line)
    line.summary_client = ''
    line.save(update_fields=['answers', 'answers_raw', 'summary_server', 'summary_client'])


def _erase_tracks(quote: QuoteSubmission) -> None:
    """The engine's silent Track rows of this quote keep the service and stage, not the client's name/email."""
    tids = [tid for tid in ServiceLink.objects.filter(Q(source_key=f'qr:{quote.pk}') | Q(quote=quote))
            .values_list('tracking_id', flat=True) if tid]
    for track in Track.objects.filter(Q(tid__in=tids) | Q(data__source_key=f'qr:{quote.pk}')):
        data = dict(track.data or {})
        if data.get('name') or data.get('email'):
            track.data = {**data, 'name': '', 'email': ''}
            track.save(update_fields=['data', 'updated_at'])


def erase_zoho_record(record_id: str) -> int:
    """`quotes_erase --zoho-record`: the v3 quote of that GAQL id, or the crm rows of a legacy source."""
    erased = 0
    for quote in QuoteSubmission.objects.filter(zoho_record_id=record_id):
        erase_quote_pii(quote)
        erased += 1
    source = Q(source_key=f'rel:gaql:{record_id}') | _record_q(record_id)
    with transaction.atomic():
        erased += _erase_crm_rows(source, source, source)
    logger.info('zoho_record_pii_erased rows=%s', erased)
    return erased


def _record_q(record_id: str) -> Q:
    return Q(source_record_id=record_id) if record_id else Q(pk__in=[])


def _erase_crm_rows(links: Q, requests: Q, signals: Q) -> int:
    """ServiceLink `candidates[].name` + `actor.email`; EngineRequest `result`/`actor`/`ip`; signal `*_raw`."""
    count = 0
    for link in ServiceLink.objects.filter(links):
        link.candidates = [{**item, 'name': ''} if isinstance(item, dict) else item for item in link.candidates or []]
        link.actor = {**(link.actor or {}), 'email': ''} if link.actor else {}
        link.save(update_fields=['candidates', 'actor', 'updated_at'])
        count += 1
    count += EngineRequest.objects.filter(requests).update(result=None, actor={}, ip=None)
    for signal in ServiceSignal.objects.filter(signals):
        data = signal.data or {}
        signal.data = {key: value for key, value in data.items() if not key.endswith('_raw')}
        signal.save(update_fields=['data'])
        count += 1
    return count


def _without_payloads(amendments: list) -> list:
    return [{**entry, 'payload': None} if isinstance(entry, dict) and entry.get('payload') else entry
            for entry in amendments]
