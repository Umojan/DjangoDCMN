"""Ops tasks: beat heartbeat, Zoho token pre-warm, engine PII purge (DESIGN §7.8, §4.5), daily digest (§12.2)."""

import logging
import time
from datetime import date, datetime, timedelta
from datetime import time as day_start

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

from quotes import flags as quote_flags
from quotes.models import QuoteLine, QuoteRejection, QuoteSubmission
from quotes.phone_match import followup_calls_on
from quotes.render import ET

from .. import flags, zoho
from ..alerts import BEAT_HEARTBEAT_KEY, alert_ops
from ..auth import ignored_tokens_on
from ..messages import trigger_word
from ..models import EngineRequest, ServiceLink, ServiceSignal
from ..translation_capture import ignored_counts
from .links import ORPHAN_CACHE_KEY

logger = logging.getLogger(__name__)

HEARTBEAT_TTL_S = 24 * 3600
TOKEN_LOCK_KEY = 'crm:zoho:token_lock'
TOKEN_LOCK_TTL_S = 60
PII_RETENTION = timedelta(days=90)
TERMINAL_LINK_STATES = ('created', 'adopted', 'dismissed', 'failed', 'target_deleted')
DONE_SIGNAL_STATES = ('evaluated', 'expired', 'error')   # terminal: the raw language text is no longer read
OPEN_LINK_STATES = ('failed', 'needs_decision', 'degraded')
OPEN_AFTER = timedelta(hours=24)
DIGEST_MAX_ITEMS = 50


@shared_task(name='crm.tasks.beat_heartbeat')
def beat_heartbeat() -> None:
    """Beat, every 60 s: proves beat is alive to crm.alerts.check_beat()."""
    cache.set(BEAT_HEARTBEAT_KEY, time.time(), HEARTBEAT_TTL_S)


@shared_task(name='crm.tasks.send_ops_alert', autoretry_for=(Exception,), retry_backoff=60, max_retries=3)
def send_ops_alert(subject: str, body: str, recipients: list[str]) -> str:
    """An ops alert a button call could not send within its 10 s (already scrubbed and deduplicated)."""
    EmailMessage(subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=recipients).send()
    return 'sent'


@shared_task(name='crm.tasks.prewarm_zoho_token')
def prewarm_zoho_token() -> str:
    """Beat, every 2,400 s: refresh the Zoho token so inline clicks find it warm (only while a feature is on)."""
    if not flags.zoho_background_enabled():
        return 'skipped'
    if zoho.token_cooling_down():   # Zoho throttled the refresh: asking again only extends the block
        return 'cooldown'
    if not cache.add(TOKEN_LOCK_KEY, 1, TOKEN_LOCK_TTL_S):
        return 'locked'
    try:
        zoho.refresh_token(zoho.LEGACY_TOKEN_TIMEOUT)
    except (ZohoTransientError, ZohoPermanentError) as exc:
        logger.warning('zoho_token_prewarm_failed code=%s', getattr(exc, 'code', type(exc).__name__))
        return 'failed'
    return 'refreshed'


@shared_task(name='crm.tasks.purge_engine_pii')
def purge_engine_pii() -> str:
    """Beat, daily 03:40 UTC: after 90 days null EngineRequest.result/ip, blank actor.email, blank candidate names
    on terminal links and drop `*_raw` from evaluated or expired signals."""
    cutoff = timezone.now() - PII_RETENTION
    requests = 0
    old = EngineRequest.objects.filter(created_at__lt=cutoff).filter(
        Q(result__isnull=False) | Q(ip__isnull=False) | Q(actor__has_key='email'))
    for request in old.iterator():
        actor = {**request.actor, 'email': ''} if (request.actor or {}).get('email') else request.actor
        if request.result is None and request.ip is None and actor == request.actor:
            continue
        EngineRequest.objects.filter(pk=request.pk).update(result=None, ip=None, actor=actor)
        requests += 1

    links = 0
    for link in ServiceLink.objects.filter(updated_at__lt=cutoff, state__in=TERMINAL_LINK_STATES).iterator():
        candidates = [{**candidate, 'name': ''} if candidate.get('name') else candidate
                      for candidate in link.candidates or []]
        actor = {**link.actor, 'email': ''} if (link.actor or {}).get('email') else link.actor
        if candidates != (link.candidates or []) or actor != link.actor:
            ServiceLink.objects.filter(pk=link.pk).update(candidates=candidates, actor=actor)
            links += 1

    signals = 0
    for signal in ServiceSignal.objects.filter(created_at__lt=cutoff, state__in=DONE_SIGNAL_STATES).iterator():
        data = {key: value for key, value in (signal.data or {}).items() if not key.endswith('_raw')}
        if data != (signal.data or {}):
            signal.data = data
            signal.save(update_fields=['data'])
            signals += 1
    logger.info('engine_pii_purged requests=%s links=%s signals=%s', requests, links, signals)
    return f'requests={requests} links={links} signals={signals}'


@shared_task(name='crm.tasks.ops_daily_digest')
def ops_daily_digest() -> str:
    """Beat, daily 13:00 UTC: yesterday (ET) in numbers plus every open item, emailed to OPS_ALERT_EMAILS.

    References, ids, codes and admin links only (invariant 9); never a name, email or phone. Sent only while
    a feature is on: with every flag at its default (S0) nothing is mailed.
    """
    if not (flags.zoho_background_enabled() or flags.translation_capture_enabled() or quote_flags.shadow_legacy()):
        return 'skipped'
    day = (timezone.now().astimezone(ET) - timedelta(days=1)).date()
    body = digest_text(day)
    sent = alert_ops('daily_digest', day.isoformat(), f'Daily digest {day:%a %d %b %Y}', body)
    logger.info('ops_digest day=%s sent=%s', day.isoformat(), sent)
    return 'sent' if sent else 'not_sent'


def digest_text(day: date) -> str:
    """The digest body for one ET day."""
    start = datetime.combine(day, day_start.min, tzinfo=ET)
    in_day = {'created_at__gte': start, 'created_at__lt': start + timedelta(days=1)}
    stale = timezone.now() - OPEN_AFTER
    quotes = QuoteSubmission.objects.filter(**in_day)
    links_done = ServiceLink.objects.filter(state__in=('created', 'adopted'), done_at__gte=start,
                                            done_at__lt=start + timedelta(days=1))
    sections = [
        (f'Quote submissions {day:%d %b}', _counts(quotes.filter(is_test=False), 'sync_status')
         + [f'test (ZZ TEST): {quotes.filter(is_test=True).count()}']),
        ('Spam status', _counts(quotes, 'spam_status')),
        ('Spam rejections by reason', _counts(QuoteRejection.objects.filter(**in_day), 'reason')),
        ('Engine requests by result', _counts(EngineRequest.objects.filter(**in_day), 'caller', 'state')),
        ('Engine creates per source', [f'{kind} · {trigger_word(trigger)}: {n}' for kind, trigger, n in
                                       links_done.values_list('source_kind', 'trigger').annotate(n=Count('id'))
                                       .order_by('source_kind', 'trigger')]),
        ('Open services older than 24 h', [
            f'{link.service_key} {link.state} · {link.source_key}'
            f'{" · " + link.last_error_code if link.last_error_code else ""} · {_admin("crm_servicelink", link.pk)}'
            for link in ServiceLink.objects.filter(state__in=OPEN_LINK_STATES, updated_at__lt=stale)
            .order_by('updated_at')[:DIGEST_MAX_ITEMS]]),
        ('Quotes failed to sync', [
            f'{quote.reference} · {quote.sync_step or "-"} · {_admin("quotes_quotesubmission", quote.pk)}'
            for quote in QuoteSubmission.objects.filter(sync_status='failed').order_by('created_at')
            [:DIGEST_MAX_ITEMS]]),
        ('Orphan workflow copies (safe to delete after a check)', [
            f'{item.get("quote", "")} · {item.get("module", "")} {item.get("id", "")} · {item.get("url", "")}'
            for item in (cache.get(ORPHAN_CACHE_KEY) or [])[:DIGEST_MAX_ITEMS]]),
        ('Unapproved prices', [
            f'{line.quote.reference} · {line.service_key} · {_admin("quotes_quotesubmission", line.quote_id)}'
            for line in QuoteLine.objects.filter(price_flag='unapproved', quote__created_at__gte=start,
                                                 quote__created_at__lt=start + timedelta(days=1))
            .select_related('quote').order_by('quote_id')[:DIGEST_MAX_ITEMS]]),
        ('Tracking pages of deleted records (kept)', [
            f'{link.tracking_id} · {link.service_key} · {link.source_key} · {_admin("crm_servicelink", link.pk)}'
            for link in ServiceLink.objects.filter(state='target_deleted').exclude(tracking_id='')
            .order_by('updated_at')[:DIGEST_MAX_ITEMS]]),
        ('followup_call_record', [f'follow-up calls that created a phone record: {followup_calls_on(day)}']),
        ('translation_capture_ignored', [f'{order_type}: {n}' for order_type, n in
                                         sorted(ignored_counts(day).items())]),
        ('body_token_ignored', [f'engine API calls with a key in the body or query: {ignored_tokens_on(day)}']),
    ]
    lines = [f'DCMN ops digest for {day:%A %d %B %Y} (ET)']
    for title, items in sections:
        lines += ['', title] + [f'- {item}' for item in items or ['none']]
    return '\n'.join(lines)


def _counts(queryset, *fields: str) -> list[str]:
    rows = queryset.values_list(*fields).annotate(n=Count('pk')).order_by(*fields)
    return [f'{" · ".join(str(value or "-") for value in row[:-1])}: {row[-1]}' for row in rows]


def _admin(model: str, pk) -> str:
    base = str(getattr(settings, 'BASE_URL', '') or '').rstrip('/')
    return f'{base}{reverse(f"admin:{model}_change", args=[pk])}'
