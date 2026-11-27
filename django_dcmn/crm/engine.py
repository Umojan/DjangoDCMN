"""The related-service engine (DESIGN §7.7, §7.8): one entry point, `handle`, and the link runner `run_link`.

`handle` audits the call, applies the gates, resolves the source, expands the services and either runs one
link inline (a Zoho button, warm token, free semaphore, 6 s budget) or queues `crm.tasks.run_service_link`.
`run_link` leases a link, adopts or creates its record and saves each result right after its call: no DB
transaction is ever held across HTTP.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from quotes import contract as c
from quotes.models import QuoteLine

from . import adoption, flags, limits, messages, registry, schema, sources, tid, zoho
from .alerts import alert_ops
from .contacts import find_or_create
from .models import EngineRequest, ServiceLink
from .redact import payload_sha256, redact_result
from .registry import KEY_FIELD, NO_TARGET, ServiceSpec, UnknownService
from .sources import SourceNotAllowed, SourceNotFound, SourceRef
from .writeback import update_writeback

logger = logging.getLogger(__name__)

MODES = ('create', 'preview', 'suggest', 'dry_run')
TERMINAL_REQUEST_STATES = ('done', 'partial', 'needs_decision', 'failed', 'refused')
DEDUPE_WINDOW = timedelta(minutes=2)
LEASE = timedelta(seconds=60)
RETRY_DELAYS_S = (30, 120, 600, 1800, 3600, 7200, 14400, 28800)
MAX_ATTEMPTS = len(RETRY_DELAYS_S)
MAX_RETRY_AGE = timedelta(hours=24)
INLINE_BUDGET_S = 6.0
INLINE_CALLERS = ('zoho',)
WRITEBACK_COUNTDOWN_S = 3
TOKEN_CACHE_KEY = zoho.TOKEN_CACHE_KEY   # the legacy token entry (orders.zoho_sync)
POLL_URL = '/api/crm/v1/jobs/{job_id}/'

DONE_LINK_STATES = ('created', 'adopted')
RUNNABLE_STATES = ('pending', 'retry')
GOOD_STATUSES = frozenset({'created', 'adopted', 'exists', 'no_target', 'suggested', 'dry_run', 'preview',
                           'dismissed'})
BAD_STATUSES = frozenset({'failed', 'degraded', 'target_deleted', 'in_progress'})
LINK_STATUS = {'pending': 'queued', 'retry': 'queued', 'creating': 'queued'}


@dataclass
class EngineResult:
    http_status: int
    body: dict
    request: EngineRequest


# --- Audit ------------------------------------------------------------------------------------------

def _finish(request: EngineRequest, http_status: int, body: dict, *, state: str, error_code: str = '',
            started: float | None = None) -> EngineResult:
    """Every return path of `handle` ends here (invariant 10): state, status, redacted result, duration."""
    request.state = state
    request.http_status = http_status
    request.error_code = error_code[:40]
    request.result = redact_result(body)
    if started is not None:
        request.duration_ms = int((time.monotonic() - started) * 1000)
    if state in TERMINAL_REQUEST_STATES:
        request.finished_at = timezone.now()
    request.save(update_fields=['state', 'http_status', 'error_code', 'result', 'duration_ms', 'finished_at',
                                'source_key', 'source_module', 'source_record_id'])
    return EngineResult(http_status=http_status, body=body, request=request)


def _body(request: EngineRequest, *, ok: bool, state: str, message: str, results: list[dict] | None = None,
          open_url: str | None = None, warnings: list[str] | None = None, detail: str = '') -> dict:
    body = {'ok': ok, 'job_id': str(request.public_id), 'ref': request.ref, 'state': state, 'message': message,
            'open_url': open_url, 'results': results or [], 'warnings': warnings or []}
    if detail:
        body['detail'] = detail
    if state == 'running':
        body['poll_url'] = POLL_URL.format(job_id=request.public_id)
    return body


def _refuse(request: EngineRequest, http_status: int, code: str, message: str, started: float,
            results: list[dict] | None = None) -> EngineResult:
    body = _body(request, ok=False, state='refused', message=message, results=results, detail=code)
    return _finish(request, http_status, body, state='refused', error_code=code, started=started)


# --- handle -----------------------------------------------------------------------------------------

def handle(source: SourceRef | tuple[str, str], services: list[str], *, mode: str, trigger: str, caller: str,
           actor: dict, options: dict | None = None, client_request_id: str | None = None,
           budget_s: float | None = None, ip: str | None = None, caller_key_id: str = '') -> EngineResult:
    """Create, link, suggest or preview related service records of one source (DESIGN §7.8)."""
    started = time.monotonic()
    module, record_id = (source.module, source.record_id) if isinstance(source, SourceRef) else map(str, source)
    request = EngineRequest.objects.create(
        client_request_id=(client_request_id or '')[:140], caller=caller[:12], trigger=(trigger or '')[:40],
        mode=(mode or '')[:8], actor=actor or {}, caller_key_id=caller_key_id[:8], ip=ip,
        source_key=source.key if isinstance(source, SourceRef) else '', source_module=module or '',
        source_record_id=record_id or '', services_requested=list(services or []),
    )
    try:
        return _handle(request, source, list(services or []), mode=mode, trigger=trigger or '', caller=caller,
                       actor=actor or {}, options=dict(options or {}), started=started, budget_s=budget_s)
    except Exception:
        logger.exception('engine_error request=%s', request.ref)
        body = _body(request, ok=False, state='failed', message=messages.ZOHO_UNAVAILABLE, detail='internal')
        _finish(request, 500, body, state='failed', error_code='internal', started=started)
        raise


def _handle(request: EngineRequest, source, services: list[str], *, mode: str, trigger: str, caller: str,
            actor: dict, options: dict, started: float, budget_s: float | None) -> EngineResult:
    if request.client_request_id:
        # Only a strictly earlier twin counts: two clicks inserted together must not refuse each other.
        earlier = (EngineRequest.objects.filter(client_request_id=request.client_request_id,
                                                created_at__gte=timezone.now() - DEDUPE_WINDOW, pk__lt=request.pk)
                   .exclude(state__in=TERMINAL_REQUEST_STATES).order_by('-created_at').first())
        if earlier is not None:
            body = {'ok': True, 'job_id': str(earlier.public_id), 'ref': earlier.ref, 'state': 'running',
                    'message': messages.IN_PROGRESS, 'open_url': None,
                    'poll_url': POLL_URL.format(job_id=earlier.public_id),
                    'results': list((earlier.result or {}).get('results') or []), 'warnings': []}
            return _finish(request, 202, body, state='refused', error_code='duplicate_request', started=started)

    engine_mode = flags.engine_mode()
    if engine_mode == 'off':
        return _refuse(request, 503, 'disabled', messages.DISABLED, started)
    if mode not in MODES:
        return _refuse(request, 400, 'bad_mode', f'Unknown mode: {mode}.', started)
    try:
        sources.check_allowed(request.source_module)
    except SourceNotAllowed:
        return _refuse(request, 403, 'source_not_allowed', messages.SOURCE_NOT_ALLOWED, started)
    if caller != 'system' and zoho.circuit_is_open():
        return _refuse(request, 503, 'zoho_unavailable', messages.ZOHO_UNAVAILABLE, started)

    budget = limits.Budget(started + min(budget_s or INLINE_BUDGET_S, INLINE_BUDGET_S)
                           if caller in INLINE_CALLERS else None)
    # A cold token makes a click answer 202 (DESIGN §7.8 step 7); the refresh the source read needs stays
    # inside the inline budget instead of the legacy (5, 45) timeout.
    cold = caller in INLINE_CALLERS and not cache.get(TOKEN_CACHE_KEY)
    try:
        if cold and not isinstance(source, SourceRef):
            zoho.refresh_token(budget.timeout())
        src = source if isinstance(source, SourceRef) else sources.resolve(*source, timeout=budget.timeout())
    except SourceNotFound:
        return _refuse(request, 404, 'source_not_found', messages.SOURCE_NOT_FOUND, started)
    except zoho.ZohoTransient:
        return _refuse(request, 503, 'zoho_unavailable', messages.ZOHO_UNAVAILABLE, started)
    except zoho.ZohoPermanent as exc:
        return _refuse(request, 503, f'zoho_{exc.code}'.lower(), messages.ZOHO_UNAVAILABLE, started)
    request.source_key = src.key

    if flags.engine_test_only() and not sources.is_test(src):
        return _refuse(request, 403, 'test_only', messages.TEST_ONLY, started)

    effective = 'dry_run' if engine_mode == 'dry_run' and mode == 'create' else mode
    if '*' in services and src.kind != 'quote':
        return _refuse(request, 422, 'star_not_allowed', messages.STAR_NOT_ALLOWED, started)
    try:
        keys = _expand(src, services, options)
    except UnknownService as exc:
        return _refuse(request, 422, 'unknown_service', messages.UNKNOWN_SERVICE.format(key=exc.args[0]), started)

    if sources.is_spam(src) and not options.get('force_spam'):
        results = [{'service': key, 'status': 'refused_spam'} for key in keys]
        body = _body(request, ok=True, state='refused', message=messages.REFUSED_SPAM, results=results)
        return _finish(request, 200, body, state='refused', error_code='refused_spam', started=started)

    if effective == 'create' and _needs_create(src, keys) and not limits.check_create_limit(src.key, caller):
        return _refuse(request, 429, 'rate_limited', messages.RATE_LIMITED, started)

    requested = list(src.requested)   # before the manager lines below: they keep reason=manager
    if effective == 'create' and src.kind == 'quote':
        _add_manager_lines(src, keys)

    call = _Call(request=request, src=src, mode=effective, trigger=trigger, caller=caller, actor=actor,
                 options=options, budget=budget, requested=requested, cold=cold)
    results = [_one_service(call, key) for key in keys]
    if call.runnable:
        _run_or_queue(call, results, single=len(keys) == 1 and '*' not in services)
    if call.touched:
        _schedule_writeback(src.key)
    return _answer(call, results, started)


@dataclass
class _Call:
    request: EngineRequest
    src: SourceRef
    mode: str
    trigger: str
    caller: str
    actor: dict
    options: dict
    budget: limits.Budget
    requested: list[str]
    cold: bool = False          # the token was cold when the call came in: no inline run
    runnable: list[ServiceLink] = field(default_factory=list)
    touched: bool = False


def _latest_link(source_key: str, service_key: str) -> ServiceLink | None:
    return ServiceLink.objects.filter(source_key=source_key, service_key=service_key).order_by('-seq').first()


def _expand(src: SourceRef, services: list[str], options: dict) -> list[str]:
    keys = registry.expand([s for s in services if s != '*'], src.requested)
    if '*' in services:
        for key in registry.expand(['*'], src.requested):
            link = _latest_link(src.key, key)
            state = link.state if link else None
            if state in ('created', 'adopted', 'dismissed'):
                continue
            if state == 'suggested' and not options.get('include_suggested'):
                continue
            keys.append(key)
    return list(dict.fromkeys(keys))


def _needs_create(src: SourceRef, keys: list[str]) -> bool:
    """Whether any service may end in a real create (an `exists` answer is not counted against the limit)."""
    for key in keys:
        if key in NO_TARGET:
            continue
        link = _latest_link(src.key, key)
        if link is None or link.state not in DONE_LINK_STATES:
            return True
    return False


def _add_manager_lines(src: SourceRef, keys: list[str]) -> None:
    """A v5 service the client did not request becomes a `QuoteLine(source=manager)` (DESIGN §7.8 step 5)."""
    quote = src.quote
    for key in keys:
        if key in c.SERVICE_KEYS and key not in src.requested:
            position = (quote.lines.aggregate(top=Max('position'))['top'] or 0) + 1
            QuoteLine.objects.get_or_create(quote=quote, service_key=key,
                                            defaults={'position': position, 'source': 'manager'})
            src.requested.append(key)


# --- Per service ------------------------------------------------------------------------------------

def external_key(source_key: str, service_key: str, seq: int) -> str:
    """`dcmn:{source_key}:{service_key}[#seq]` (DESIGN §7.5)."""
    key = f'dcmn:{source_key}:{service_key}'
    return f'{key}#{seq}' if seq > 1 else key


def _reason(call: _Call, key: str) -> str:
    if call.trigger == 'intake':
        return 'intake'
    if call.trigger.startswith('rule:'):
        return f'signal:{call.trigger[5:]}'[:60]
    return 'requested' if key in call.requested else 'manager'


def _link_values(call: _Call, key: str, seq: int, state: str) -> dict:
    src = call.src
    line = src.quote.lines.filter(service_key=key).first() if src.kind == 'quote' else None
    return {
        'source_kind': src.kind, 'source_key': src.key, 'source_module': src.module,
        'source_record_id': src.record_id or '', 'quote': src.quote if src.kind == 'quote' else None,
        'quote_line': line, 'service_key': key, 'seq': seq, 'external_key': external_key(src.key, key, seq),
        'state': state, 'target_module': registry.get(key).module, 'trigger': call.trigger[:40],
        'caller': call.caller[:12], 'actor': call.actor, 'reason': _reason(call, key), 'request': call.request,
    }


def _link_for(call: _Call, key: str) -> tuple[ServiceLink | None, bool]:
    """(link, created now); (None, False) when a racing "create again" holds the next seq (in_progress)."""
    current = _latest_link(call.src.key, key)
    initial = 'suggested' if call.mode == 'suggest' else 'pending'
    if call.options.get('create_again') and current is not None and call.mode == 'create':
        try:
            with transaction.atomic():
                rows = list(ServiceLink.objects.select_for_update()
                            .filter(source_key=call.src.key, service_key=key).order_by('-seq'))
                seq = (rows[0].seq if rows else 0) + 1
                return ServiceLink.objects.create(**_link_values(call, key, seq, 'pending')), True
        except IntegrityError:
            return None, False
    if current is not None:
        return current, False
    try:
        with transaction.atomic():
            return ServiceLink.objects.create(**_link_values(call, key, 1, initial)), True
    except IntegrityError:
        return _latest_link(call.src.key, key), False


def _one_service(call: _Call, key: str) -> dict:
    if key in NO_TARGET:
        return {'service': key, 'status': 'no_target', 'module': None, 'id': None, 'url': None}
    if call.mode in ('preview', 'dry_run'):
        try:
            return _preview(call, key)
        except zoho.ZohoTransient:
            return {'service': key, 'status': 'failed', 'error_code': 'zoho_unavailable'}
        except zoho.ZohoPermanent as exc:
            return {'service': key, 'status': 'failed', 'error_code': exc.code, 'error_field': exc.field}
    link, created = _link_for(call, key)
    if link is None:
        return {'service': key, 'status': 'in_progress'}
    if call.mode == 'suggest':
        call.touched = call.touched or created
        result = link_result(link)
        return {**result, 'status': 'exists'} if link.state in DONE_LINK_STATES else result
    return _prepare_create(call, link, created)


def _preview(call: _Call, key: str) -> dict:
    """Adoption read-only + build + guard; no link is created or changed, nothing is written."""
    spec = registry.get(key)
    current = _latest_link(call.src.key, key)
    link = current or ServiceLink(source_kind=call.src.kind, source_key=call.src.key, service_key=key, seq=1,
                                  external_key=external_key(call.src.key, key, 1), state='pending')
    result = {'service': key, 'status': call.mode, 'module': spec.module, 'id': None, 'url': None,
              'link_id': current.pk if current else None}
    facts = sources.facts_for(call.src, key)
    found = adoption.find_existing(link, spec, facts, call.src, budget=call.budget, read_only=True)
    if isinstance(found, adoption.Existing):
        status = 'target_deleted' if found.deleted else 'exists'
        return {**link_result(current), 'status': status}
    if isinstance(found, adoption.Adoption):
        return {**result, 'action': 'adopt', 'id': found.record_id, 'url': spec.url(found.record_id)}
    if isinstance(found, adoption.Candidates):
        return {**result, 'action': 'needs_decision', 'candidates': found.items}
    guarded = schema.guard(spec.module, registry.build(spec, facts, source_kind=call.src.kind,
                                                       key=link.external_key), spec.blocking)
    return {**result, 'action': 'degraded' if guarded.blocked else 'create', 'stage': guarded.payload.get(
        spec.stage_field), 'payload': guarded.payload, 'dropped': [name for name, _ in guarded.dropped],
        'blocked': guarded.blocked}


def _prepare_create(call: _Call, link: ServiceLink, created: bool) -> dict:
    """Decide what a create click does with the link's current state; runnable links are collected."""
    options = call.options
    spec = registry.get(link.service_key)
    if options.get('link_existing'):
        return _link_existing(call, link, spec, str(options['link_existing']))
    if link.state in DONE_LINK_STATES:
        return _check_exists(call, link, spec)
    if link.state == 'target_deleted':
        return link_result(link)
    if link.state == 'needs_decision' and not options.get('create_new'):
        return link_result(link)
    if link.state == 'creating' and link.lease_until and link.lease_until > timezone.now():
        return {**link_result(link), 'status': 'in_progress'}

    overrides = dict(link.overrides or {})
    if options.get('create_new'):
        overrides['create_new'] = True
    if isinstance(options.get('overrides'), dict):
        overrides.update({k: v for k, v in options['overrides'].items() if k in ('stage', 'owner_id', 'comment_append')})
    if not created or overrides != (link.overrides or {}):
        values = {'state': 'pending', 'overrides': overrides, 'next_attempt_at': None, 'lease_until': None,
                  'trigger': call.trigger[:40], 'caller': call.caller[:12], 'actor': call.actor,
                  'request': call.request}
        if link.state in ('failed', 'degraded', 'dismissed', 'suggested', 'needs_decision'):
            values.update(attempts=0, last_error='', last_error_code='', candidates=[] if options.get(
                'create_new') else link.candidates)
        if not _reset(link, values):
            # A runner leased (or finished) the link since it was read: never take it from under the runner.
            link.refresh_from_db()
            result = link_result(link)
            return {**result, 'status': 'in_progress'} if link.state == 'creating' else result
    call.runnable.append(link)
    call.touched = True
    return link_result(link)


def _reset(link: ServiceLink, values: dict) -> bool:
    """Save `values` only if the link is still in the state it was read in and holds no live lease."""
    now = timezone.now()
    updated = (ServiceLink.objects.filter(pk=link.pk, state=link.state)
               .exclude(state='creating', lease_until__gt=now).update(**values, updated_at=now))
    if updated:
        for name, value in values.items():
            setattr(link, name, value)
        link.updated_at = now
    return bool(updated)


def _check_exists(call: _Call, link: ServiceLink, spec: ServiceSpec) -> dict:
    """§7.6 step 1: the own record still exists (`exists`) or was deleted (`target_deleted`)."""
    try:
        record = zoho.get(spec.module, link.target_record_id, fields=['id'], timeout=call.budget.timeout())
    except (zoho.ZohoTransient, zoho.ZohoPermanent):
        return {**link_result(link), 'status': 'exists'}   # cannot check now; Django says it exists
    if record is None:
        _save(link, state='target_deleted', request=call.request)
        call.touched = True
        return link_result(link)
    return {**link_result(link), 'status': 'exists'}


def _link_existing(call: _Call, link: ServiceLink, spec: ServiceSpec, record_id: str) -> dict:
    """The panel's decision: link this record (adopted_manual, or adopted_phone for a phone-lead candidate)."""
    try:
        record = zoho.get(spec.module, record_id, timeout=call.budget.timeout())
        if record is None:
            return {**link_result(link), 'status': 'failed', 'error_code': 'not_found'}
        facts = sources.facts_for(call.src, link.service_key)
        adoption.enrich(link, spec, facts, record, budget=call.budget, source_kind=call.src.kind)
    except zoho.ZohoTransient:
        return {**link_result(link), 'status': 'failed', 'error_code': 'zoho_unavailable'}
    except zoho.ZohoPermanent as exc:
        return {**link_result(link), 'status': 'failed', 'error_code': exc.code}
    phone = any(c.get('id') == record_id and c.get('match') == 'phone_lead' for c in link.candidates or [])
    _save(link, state='adopted', origin='adopted_phone' if phone else 'adopted_manual', target_module=spec.module,
          target_record_id=record_id, target_url=spec.url(record_id), done_at=timezone.now(), lease_until=None,
          request=call.request, trigger=call.trigger[:40], caller=call.caller[:12], actor=call.actor)
    call.touched = True
    return {**link_result(link), 'status': 'adopted'}


def _run_or_queue(call: _Call, results: list[dict], *, single: bool) -> None:
    """One service from a Zoho button runs inline when allowed; everything else is queued."""
    inline = (single and len(call.runnable) == 1 and call.caller in INLINE_CALLERS and call.trigger != 'intake'
              and flags.engine_inline() and not call.cold and cache.get(TOKEN_CACHE_KEY))
    if inline:
        link = call.runnable[0]
        with limits.inline_slot() as acquired:
            if acquired:
                _run(link.pk, budget=call.budget, source=call.src)
                link.refresh_from_db()
                _replace(results, link_result(link))
                return
    for link in call.runnable:
        _publish(link.pk)


def _replace(results: list[dict], new: dict) -> None:
    for index, result in enumerate(results):
        if result.get('service') == new['service']:
            results[index] = new


def _publish(link_id: int) -> None:
    from .tasks.links import run_service_link

    transaction.on_commit(lambda: run_service_link.delay(link_id))


def _schedule_writeback(source_key: str) -> None:
    from .tasks.writeback import writeback_source

    transaction.on_commit(lambda: writeback_source.apply_async((source_key,), countdown=WRITEBACK_COUNTDOWN_S))


def overall_state(results: list[dict]) -> str:
    statuses = {r.get('status') for r in results}
    if 'queued' in statuses:
        return 'running'
    if statuses and statuses <= {'refused_spam'}:
        return 'refused'
    if 'needs_decision' in statuses and not statuses & BAD_STATUSES:
        return 'needs_decision'
    if statuses <= GOOD_STATUSES:
        return 'done'
    if statuses <= BAD_STATUSES:
        return 'failed'
    return 'partial'


def open_url(results: list[dict]) -> str | None:
    """Set only when exactly one record is the answer (created, adopted, exists, or the newest candidate)."""
    records = [r['url'] for r in results if r.get('status') in ('created', 'adopted', 'exists') and r.get('url')]
    deciding = [r for r in results if r.get('status') == 'needs_decision']
    if len(records) == 1 and not deciding:
        return records[0]
    if not records and len(deciding) == 1:
        candidates = sorted(deciding[0].get('candidates') or [], key=lambda c: c.get('created') or '', reverse=True)
        return candidates[0].get('url') if candidates else None
    return None


def _answer(call: _Call, results: list[dict], started: float) -> EngineResult:
    request, state = call.request, overall_state(results)
    if state == 'running':
        http_status = 202
    elif len(results) == 1 and results[0].get('status') == 'in_progress':
        http_status = 409
    else:
        http_status = 200
    message = messages.message_for(results, mode=call.mode, ref=request.ref, ref_label=call.src.ref_label)
    body = _body(request, ok=http_status < 400, state=state, message=message, results=results,
                 open_url=open_url(results) if state != 'running' else None)
    error_code = 'in_progress' if http_status == 409 else next(
        (r.get('error_code') or r['status'] for r in results if r.get('status') in BAD_STATUSES), '')
    return _finish(request, http_status, body, state=state, error_code=error_code or '', started=started)


# --- Results ----------------------------------------------------------------------------------------

def _error_field(link: ServiceLink) -> str:
    text = link.last_error or ''
    if link.state == 'degraded' and text.startswith('blocked: '):
        return text.removeprefix('blocked: ').split(',')[0]
    if '(' in text and text.endswith(')'):
        return text[text.rindex('(') + 1:-1]
    return ''


def link_result(link: ServiceLink | None) -> dict:
    """The per-service result of a link (DESIGN §8.2)."""
    if link is None:
        return {}
    status = LINK_STATUS.get(link.state, link.state)
    result = {'service': link.service_key, 'status': status, 'module': link.target_module or None,
              'id': link.target_record_id or None, 'url': link.target_url or None,
              'tracking_id': link.tracking_id or None, 'link_id': link.pk}
    if link.state == 'adopted':
        result['origin'] = link.origin
    if link.state in DONE_LINK_STATES:
        result['created_at'] = (link.done_at or link.updated_at or timezone.now()).isoformat()
        result['trigger'] = link.trigger
    if link.state == 'needs_decision':
        result['candidates'] = list(link.candidates or [])
    if link.state in ('failed', 'degraded'):
        result['error_code'] = link.last_error_code or link.state
        result['error_field'] = _error_field(link)
    return result


def refresh_request(request: EngineRequest | None) -> None:
    """After an async run: the polled job shows each link's current outcome."""
    if request is None:
        return
    stored = dict(request.result or {})
    by_service = {link.service_key: link_result(link) for link in request.links.all()}
    results = [by_service.pop(r.get('service'), r) for r in stored.get('results') or []] + list(by_service.values())
    state = overall_state(results)
    stored.update(results=results, state=state, message=messages.message_for(
        results, mode=request.mode, ref=request.ref, ref_label=''))
    request.result = redact_result(stored)
    request.state = state
    fields = ['result', 'state']
    if state in TERMINAL_REQUEST_STATES and request.finished_at is None:
        request.finished_at = timezone.now()
        fields.append('finished_at')
    request.save(update_fields=fields)


# --- run_link ---------------------------------------------------------------------------------------

def _save(link: ServiceLink, **fields) -> None:
    for name, value in fields.items():
        setattr(link, name, value)
    link.save(update_fields=[*fields, 'updated_at'])


def lease(link_id: int) -> bool:
    """`UPDATE … SET state='creating', lease_until=now()+60s, attempts=attempts+1 WHERE …` (DESIGN §7.7)."""
    now = timezone.now()
    return bool(ServiceLink.objects.filter(pk=link_id, state__in=RUNNABLE_STATES)
                .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
                .update(state='creating', lease_until=now + LEASE, attempts=F('attempts') + 1, updated_at=now))


def run_link(link_id: int, *, deadline: float | None = None) -> str:
    """Lease one link and adopt or create its record; returns the final link state."""
    return _run(link_id, budget=limits.Budget(deadline))


def _run(link_id: int, *, budget: limits.Budget, source: SourceRef | None = None) -> str:
    if flags.engine_mode() != 'live' or not lease(link_id):   # dry_run and off write nothing (DESIGN D13)
        link = ServiceLink.objects.filter(pk=link_id).first()
        return link.state if link else 'missing'
    link = ServiceLink.objects.select_related('quote', 'request').get(pk=link_id)
    try:
        _attempt(link, budget, source)
    except limits.BudgetExhausted:
        # Out of inline budget before a call was started: no attempt is spent, the task takes over now.
        _save(link, state='pending', lease_until=None, attempts=max(link.attempts - 1, 0))
        _publish(link.pk)
    except zoho.ZohoTransient as exc:
        _retry_or_fail(link, exc.code or 'TRANSIENT', getattr(exc, 'field', ''))
    except zoho.ZohoPermanent as exc:
        _fail(link, exc.code or 'PERMANENT', exc.field)
    except (SourceNotAllowed, SourceNotFound) as exc:
        _fail(link, 'source_not_allowed' if isinstance(exc, SourceNotAllowed) else 'source_not_found', '')
    except Exception:
        logger.exception('engine_run_error link=%s', link.pk)
        _retry_or_fail(link, 'internal', '')
    finally:
        _schedule_writeback(link.source_key)
        refresh_request(link.request)
    return link.state


def _attempt(link: ServiceLink, budget: limits.Budget, source: SourceRef | None) -> None:
    spec = registry.get(link.service_key)
    src = source or sources.for_link(link, timeout=budget.timeout())
    facts = sources.facts_for(src, link.service_key)
    found = adoption.find_existing(link, spec, facts, src, budget=budget)
    if isinstance(found, adoption.Existing):
        _save(link, state='target_deleted' if found.deleted else 'created', lease_until=None)
        return
    if isinstance(found, adoption.Adoption):
        record = found.record if found.record and found.record.get('id') else zoho.get(
            spec.module, found.record_id, timeout=budget.timeout())
        if record is not None and _own_lost_create(link, found, record):
            # Our own earlier POST made it and its answer was lost: finish it as the create it was.
            logger.info('engine_lost_create_resolved link=%s module=%s id=%s', link.pk, spec.module, found.record_id)
            _created(link, spec, found.record_id, new=True)
            return
        if record is not None and (found.origin != 'adopted_copy' or _claim_copy(link, spec, found.record_id)):
            _adopt(link, spec, facts, src, found, record, budget)
            return
    if isinstance(found, adoption.Candidates):
        _save(link, state='needs_decision', candidates=found.items, lease_until=None)
        return
    _create(link, spec, facts, src, budget)


def _own_lost_create(link: ServiceLink, found: adoption.Adoption, record: dict) -> bool:
    """A key hit on the record this link's own earlier POST made (the POST was sent: payload_sha256 is saved
    right before it, or the record carries the link's silent Tracking ID). A key found without a POST of ours
    (a restored database) stays `adopted_key`."""
    if found.origin != 'adopted_key':
        return False
    if link.tracking_id and str(record.get('Tracking_ID') or '') == link.tracking_id:
        return True
    return bool(link.payload_sha256)


def _claim_copy(link: ServiceLink, spec: ServiceSpec, record_id: str) -> bool:
    """Claim a workflow copy for this link before it is enriched; False when another link holds it.

    Two links of one module (fbi + fingerprinting, two notary keys) run in parallel after "Create all"; both see
    the same free copy, so the claim is taken under a lock on the source's links of that module.
    """
    with transaction.atomic():
        list(ServiceLink.objects.select_for_update().filter(source_key=link.source_key, target_module=spec.module)
             .values_list('pk', flat=True))
        taken = (ServiceLink.objects.filter(target_module=spec.module, target_record_id=record_id)
                 .exclude(pk=link.pk).exists())
        if not taken:
            ServiceLink.objects.filter(pk=link.pk).update(target_module=spec.module, target_record_id=record_id,
                                                          updated_at=timezone.now())
    if taken:
        logger.info('engine_copy_taken link=%s module=%s id=%s', link.pk, spec.module, record_id)
        return False
    link.target_module, link.target_record_id = spec.module, record_id
    return True


def _adopt(link: ServiceLink, spec: ServiceSpec, facts, src: SourceRef, found: adoption.Adoption, record: dict,
           budget: limits.Budget) -> None:
    adoption.enrich(link, spec, facts, record, budget=budget, source_kind=src.kind)
    if found.duplicates:   # merged into the stored dict: a running write-back keeps its own keys
        update_writeback(link, lambda stored: {**stored, 'duplicate_copies': found.duplicates})
    _save(link, state='adopted', origin=found.origin, target_module=spec.module, target_record_id=found.record_id,
          target_url=spec.url(found.record_id), done_at=timezone.now(), lease_until=None, last_error='',
          last_error_code='',
          target_key_written=link.target_key_written or record.get(KEY_FIELD) == link.external_key)
    logger.info('engine_adopted link=%s module=%s id=%s origin=%s', link.pk, spec.module, found.record_id,
                found.origin)


def _contact(link: ServiceLink, facts, budget: limits.Budget) -> None:
    """A source without a contact lookup gets the shared find-or-create (DESIGN §7.2); never inline."""
    client = facts.client
    if client.get('contact_id') or not (client.get('email') or client.get('phone_raw')):
        return
    if budget.deadline is not None:
        raise limits.BudgetExhausted('contact lookup runs in the task', code='BUDGET')
    contact_id, _ = find_or_create(client.get('name') or '', client.get('email'),
                                   client.get('phone_e164') or client.get('phone_raw'))
    client['contact_id'] = contact_id


def _create(link: ServiceLink, spec: ServiceSpec, facts, src: SourceRef, budget: limits.Budget) -> None:
    _contact(link, facts, budget)
    overrides = link.overrides or {}
    extra = [str(overrides['comment_append'])[:500]] if overrides.get('comment_append') else []
    payload = registry.build(spec, facts, source_kind=src.kind, key=link.external_key, extra_comments=extra,
                             stage=overrides.get('stage') or None)
    if overrides.get('owner_id'):
        payload['Owner'] = {'id': str(overrides['owner_id'])}
    guarded = schema.guard(spec.module, payload, spec.blocking)
    if guarded.blocked:
        _degrade(link, spec, guarded.blocked)
        return
    record = dict(guarded.payload)
    if guarded.dropped and spec.client.comments in record:
        record[spec.client.comments] = _with_dropped(record[spec.client.comments], guarded.dropped,
                                                     registry.field_length(spec.module, spec.client.comments))
    tracking_id = tid.mint_silent(spec.module, spec.key, facts, link)
    if tracking_id:
        record['Tracking_ID'] = tracking_id
    _save(link, payload_sha256=payload_sha256(record))
    result = zoho.create_once(spec.module, record, link.external_key, trigger=flags.trigger_for(spec.module),
                              timeout=budget.timeout())
    _created(link, spec, result.id, new=result.created)


def _created(link: ServiceLink, spec: ServiceSpec, record_id: str, *, new: bool) -> None:
    """The record exists: the link is `created` first, then the bookkeeping, which can never undo that."""
    _save(link, state='created', origin='created', target_module=spec.module, target_record_id=record_id,
          target_url=spec.url(record_id), target_key_written=True, done_at=timezone.now(), lease_until=None,
          last_error='', last_error_code='')
    logger.info('engine_created link=%s module=%s id=%s new=%s', link.pk, spec.module, record_id, new)
    steps = [('tid_record_id', tid.set_record_id), ('tid_created_email', tid.maybe_send_created_email)]
    if new:
        steps.insert(0, ('count_create', lambda l: limits.count_create(l.source_key, l.caller)))
    for name, step in steps:
        try:
            step(link)
        except Exception:
            logger.exception('engine_created_bookkeeping_failed link=%s step=%s', link.pk, name)
            try:
                alert_ops('created_bookkeeping', f'{link.external_key}:{name}',
                          f'Engine create bookkeeping failed: {link.service_key}',
                          f'{spec.module} record {record_id} of {link.external_key} was created and linked, but '
                          f'the {name} step failed (see the worker log). The record is correct; check the '
                          'tracking page and the created email of this quote by hand.')
            except Exception:
                logger.exception('engine_bookkeeping_alert_failed link=%s', link.pk)


def _with_dropped(comments: str, dropped: list[tuple[str, str]], limit: int) -> str:
    line = 'Not recorded in Zoho fields: ' + ', '.join(f"{name} '{value}'" for name, value in dropped)
    text = f'{comments}\n{line}'
    return text if len(text) <= limit else comments


def _degrade(link: ServiceLink, spec: ServiceSpec, blocked: list[str]) -> None:
    _save(link, state='degraded', lease_until=None, last_error=f'blocked: {",".join(blocked)}',
          last_error_code='schema_drift')
    logger.warning('engine_degraded link=%s module=%s fields=%s', link.pk, spec.module, ','.join(blocked))
    alert_ops('schema_drift', spec.module, f'Zoho schema drift: {spec.label} creates stopped',
              f'{spec.module}: the guard blocked {", ".join(blocked)} for link {link.external_key} '
              f'({link.request.ref if link.request else "no request"}). Nothing was created. '
              'Fix Zoho (or crm/registry.py by PR); the reconciler re-arms the link once validation passes.')


def _retry_or_fail(link: ServiceLink, code: str, field: str) -> None:
    started = link.request.created_at if link.request else link.created_at
    if link.attempts >= MAX_ATTEMPTS or timezone.now() - started > MAX_RETRY_AGE:
        _fail(link, code, field)
        return
    delay = RETRY_DELAYS_S[min(max(link.attempts - 1, 0), MAX_ATTEMPTS - 1)]
    _save(link, state='retry', lease_until=None, next_attempt_at=timezone.now() + timedelta(seconds=delay),
          last_error_code=code[:40], last_error=f'{code} ({field})' if field else code)
    logger.info('engine_retry link=%s code=%s attempts=%s', link.pk, code, link.attempts)


def _fail(link: ServiceLink, code: str, field: str) -> None:
    _save(link, state='failed', lease_until=None, last_error_code=code[:40],
          last_error=f'{code} ({field})' if field else code)
    tid.void_orphan_track(link)
    logger.warning('engine_failed link=%s code=%s', link.pk, code)
    alert_ops('link_failed', link.external_key, f'Engine create failed: {link.service_key}',
              f'Link {link.external_key} failed with {code}' + (f' ({field})' if field else '') +
              f' after {link.attempts} attempt(s). Nothing was duplicated. Retry it from the admin once fixed.')
