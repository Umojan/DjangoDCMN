"""S1 shadow mapping of a legacy `orders.QuoteRequest` (DESIGN §13.2 S1): Zoho GETs only, no writes.

The legacy GAQL record is read as the engine reads a `rel:gaql` source (`crm.sources.resolve`). For the
service of its bucket the shadow runs `crm.adoption` step 3 (the workflow copy named by the GAQL timeline,
`timeline_copy`) and step 4 (the strong heuristic confirmed by the target's timeline, `heuristic_copy`), each
on its own, and records whether they agree. The legacy quote mapped to the v3 GAQL fields and the target
record the engine would create both go through the schema guard. A `ShadowRun` stores ids, field names and
lengths, never a client value.
"""

import logging

from crm import adoption, registry, schema, sources, zoho
from crm.engine import external_key
from crm.limits import Budget
from crm.models import ServiceLink
from crm.redact import redact_result
from crm.schema import GuardResult
from orders.models import QuoteRequest, ZohoSyncJob

from . import contract as c
from .gaql import GAQL_BLOCKING, WEB_QUOTE_REQUEST
from .models import GAQL_MODULE, ShadowRun

logger = logging.getLogger(__name__)

LEGACY_DISPLAY_KEYS = {'notary': 'mobile-notary'}   # the legacy `Notary Service` bucket names no notary kind


def legacy_key(quote_request: QuoteRequest) -> str:
    return f'dcmn:quote:{quote_request.pk}'


def shadow_map(quote_request_id: int) -> ShadowRun:
    """Build and save the ShadowRun of one legacy quote; Zoho errors are stored as codes."""
    run = ShadowRun(quote_request_id=quote_request_id)
    quote_request = QuoteRequest.objects.filter(pk=quote_request_id).first()
    if quote_request is None:
        run.error = 'quote_request_missing'
        run.save()
        return run
    guarded = schema.guard(GAQL_MODULE, legacy_gaql_record(quote_request), GAQL_BLOCKING)
    preview: dict = {'payload': guarded.payload}
    run.schema_warnings = guard_warnings(guarded)
    try:
        run.gaql_record_id = gaql_record_id(quote_request)
        service = sources.BUCKET_SERVICES.get(quote_request.services or '')
        if not run.gaql_record_id:
            run.error = 'gaql_record_not_found'
        elif service:
            _compare_copies(run, preview, registry.get(service))
    except sources.SourceNotFound:
        run.error = 'gaql_record_not_found'
    except sources.SourceNotAllowed:
        run.error = 'source_not_allowed'
    except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
        run.error = exc.code or type(exc).__name__
        logger.warning('quote_shadow_failed quote_request=%s code=%s', quote_request_id, run.error)
    run.payload_preview = redact_result(preview)
    run.save()
    logger.info('quote_shadow_run quote_request=%s agree=%s', quote_request_id, run.agree)
    return run


def _compare_copies(run: ShadowRun, preview: dict, spec: registry.ServiceSpec) -> None:
    """Adoption steps 3 and 4 for one service, read-only, plus the guarded target payload."""
    source = sources.resolve(GAQL_MODULE, run.gaql_record_id)
    facts = sources.facts_for(source, spec.key)
    link = ServiceLink(source_key=source.key, service_key=spec.key, seq=1,
                       external_key=external_key(source.key, spec.key, 1))   # never saved
    target = schema.guard(spec.module, registry.build(spec, facts, source_kind=source.kind, key=link.external_key),
                          spec.blocking)
    preview['target'] = {'module': spec.module, 'payload': target.payload}
    run.schema_warnings += guard_warnings(target, prefix=f'{spec.module}.')

    events = zoho.timeline(GAQL_MODULE, run.gaql_record_id)
    copies = adoption.workflow_copies(events, spec.module)
    run.timeline_copy = {'module': spec.module, 'id': copies[0][1], 'copies': len(copies)} if copies else None
    heuristic = adoption.strong_heuristic(link, spec, facts, source, events, Budget())
    if heuristic.copy_like:
        run.heuristic_copy = {'module': spec.module, 'id': str((heuristic.confirmed or {}).get('id') or ''),
                              'matches': len(heuristic.copy_like), 'by': spec.search_by}
    run.agree = _agree(run.timeline_copy, run.heuristic_copy)


def guard_warnings(result: GuardResult, prefix: str = '') -> list[str]:
    return ([f'blocked:{prefix}{name}' for name in result.blocked]
            + sorted({f'dropped:{prefix}{name}' for name, _ in result.dropped}))


def legacy_gaql_record(quote_request: QuoteRequest) -> dict:
    """The legacy quote mapped to the v3 GAQL fields of DESIGN §5.3 (the bucket is `Web Quote Request`)."""
    service = sources.BUCKET_SERVICES.get(quote_request.services or '')
    display = c.SERVICE_DISPLAY.get(LEGACY_DISPLAY_KEYS.get(service, service)) if service else None
    reference = f'Quote ID{quote_request.pk}'
    record = {
        'Name': f'{reference} · {quote_request.name}'[:120],
        'External_Order_Key': legacy_key(quote_request),
        'Quote_Reference': reference,
        'GET_A_QUOTE_LEADS': WEB_QUOTE_REQUEST,
        'Quote_Status': 'New',
        'Primary_Service': display,
        'Services_Requested': [display] if display else [],
        'Client_Name': quote_request.name,
        'Client_Email': quote_request.email,
        'Client_Phone': quote_request.phone[:30],
        'Client_Comments': quote_request.comments or '',
    }
    if str(quote_request.number).strip().isdigit() and service in ('apostille', 'embassy'):
        record['Number_Of_Documents'] = int(str(quote_request.number).strip())
    return {name: value for name, value in record.items() if value is not None}


def gaql_record_id(quote_request: QuoteRequest) -> str:
    """From the legacy sync job, else by the legacy key (a GET search)."""
    job = (ZohoSyncJob.objects.filter(order_type='quote', order_id=quote_request.pk).exclude(zoho_record_id='')
           .order_by('-pk').first())
    if job is not None:
        return job.zoho_record_id
    record = zoho.search_by_key(GAQL_MODULE, legacy_key(quote_request))
    return str(record['id']) if record else ''


def _agree(timeline: dict | None, heuristic: dict | None) -> bool | None:
    """None when neither found a copy; True when both name the same record."""
    timeline_id = (timeline or {}).get('id') or ''
    heuristic_id = (heuristic or {}).get('id') or ''
    if not timeline_id and not heuristic_id:
        return None
    return timeline_id == heuristic_id
