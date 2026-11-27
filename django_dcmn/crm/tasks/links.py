"""Service-link runner, reconciler and orphan-copy scan (DESIGN §7.7, §7.6 step 3)."""

import logging
from datetime import timedelta

from celery import shared_task
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from orders.zoho_errors import ZohoPermanentError, ZohoTransientError
from quotes.models import GAQL_MODULE, QuoteSubmission

from .. import engine, flags, registry, schema, zoho
from ..adoption import workflow_copies
from ..models import EngineRequest, ServiceLink
from ..writeback import needs_retry
from .writeback import writeback_source

logger = logging.getLogger(__name__)

FRESH_PENDING = timedelta(seconds=60)   # a new pending link's own task is usually still in the queue
WRITEBACK_RETRY_WINDOW = timedelta(days=2)
ORPHAN_WINDOW = timedelta(hours=48)
ORPHAN_MAX_QUOTES = 50
ORPHAN_CACHE_KEY = 'crm:orphan_copies'   # read by the daily digest (WP6)
ORPHAN_CACHE_TTL_S = 2 * 24 * 3600


@shared_task(name='crm.tasks.run_service_link', acks_late=True, max_retries=0)
def run_service_link(link_id: int) -> str:
    """Create or adopt the target record of one ServiceLink; retries come through the ladder and the reconciler.

    Only while the engine is live: `dry_run` writes nothing (DESIGN D13), so a queued link waits for `live`.
    """
    if flags.engine_mode() != 'live':
        return 'skipped'
    if not ServiceLink.objects.filter(pk=link_id).exists():
        return 'missing'
    return engine.run_link(link_id)


@shared_task(name='crm.tasks.reconcile_service_links')
def reconcile_service_links() -> str:
    """Beat, every 120 s: republish due links, recover expired leases, re-arm degraded links, retry write-backs.

    Only while the engine is live (`dry_run` is a rollback that must stop every create).
    """
    if flags.engine_mode() != 'live':
        return 'skipped'
    now = timezone.now()
    expired = ServiceLink.objects.filter(state='creating', lease_until__lt=now)
    recovered = expired.update(state='retry', lease_until=None, next_attempt_at=now, updated_at=now)

    due = (ServiceLink.objects.filter(state__in=engine.RUNNABLE_STATES)
           .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
           .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
           .exclude(state='pending', updated_at__gt=now - FRESH_PENDING))
    due_ids = list(due.values_list('pk', flat=True)[:200])

    rearmed = []
    degraded = list(ServiceLink.objects.filter(state='degraded'))
    if degraded:
        metadata = {module: fields for module in schema.REFRESH_MODULES
                    if (fields := schema.get_fields(module)) is not None}
        for link in degraded:
            spec = registry.REGISTRY.get(link.service_key)
            if spec and spec.module in metadata and not registry.service_problems(spec, metadata[spec.module]):
                rearmed.append(link.pk)
    if rearmed:
        # The audit row of the re-arm; it also restarts the links' 24 h retry window.
        request = EngineRequest.objects.create(caller='system', trigger='rearm_degraded', mode='create',
                                               state='running', services_requested=sorted(
                                                   {link.service_key for link in degraded if link.pk in rearmed}))
        ServiceLink.objects.filter(pk__in=rearmed, state='degraded').update(
            state='pending', next_attempt_at=None, lease_until=None, attempts=0, request=request, updated_at=now)
    for link_id in due_ids + rearmed:
        run_service_link.delay(link_id)

    recent = ServiceLink.objects.filter(updated_at__gte=now - WRITEBACK_RETRY_WINDOW).only('source_key', 'writeback')
    sources_to_write = {link.source_key for link in recent if needs_retry(link)}
    for source_key in sorted(sources_to_write):
        writeback_source.delay(source_key)
    return f'recovered={recovered} published={len(due_ids)} rearmed={len(rearmed)} writebacks={len(sources_to_write)}'


@shared_task(name='crm.tasks.scan_orphan_copies')
def scan_orphan_copies() -> str:
    """Beat, hourly: workflow copies of recent v5 quotes that no ServiceLink adopted (ENGINE_ORPHAN_SCAN).

    Reads GAQL timelines only; the findings go to the daily digest. Nothing is written to Zoho.
    """
    if flags.engine_mode() == 'off' or not flags.orphan_scan_enabled():
        return 'skipped'
    since = timezone.now() - ORPHAN_WINDOW
    quotes = (QuoteSubmission.objects.filter(created_at__gte=since).exclude(zoho_record_id='')
              .order_by('-created_at')[:ORPHAN_MAX_QUOTES])
    modules = {spec.module for spec in registry.REGISTRY.values() if spec.copy_label}
    linked = set(ServiceLink.objects.filter(target_module__in=modules).exclude(target_record_id='')
                 .values_list('target_module', 'target_record_id'))
    orphans = []
    for quote in quotes:
        try:
            events = zoho.timeline(GAQL_MODULE, quote.zoho_record_id, max_pages=1)
        except (ZohoTransientError, ZohoPermanentError) as exc:
            logger.warning('orphan_scan_failed quote=%s code=%s', quote.pk, getattr(exc, 'code', ''))
            continue
        for module in modules:
            for _, record_id in workflow_copies(events, module):
                if (module, record_id) not in linked:
                    orphans.append({'quote': quote.reference, 'module': module, 'id': record_id,
                                    'url': zoho.record_url(registry.TABS[module], record_id)})
    cache.set(ORPHAN_CACHE_KEY, orphans, ORPHAN_CACHE_TTL_S)
    logger.info('orphan_scan quotes=%s orphans=%s', len(quotes), len(orphans))
    return f'quotes={len(quotes)} orphans={len(orphans)}'
