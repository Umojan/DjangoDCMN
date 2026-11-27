"""Find the record a link should adopt before creating one (DESIGN §7.6); enrich an adopted record.

Steps, first hit wins: 1 own link (exists / target deleted), 2 own key, 3 exact workflow copy from the GAQL
timeline, 4 strong heuristic, 5 phone-lead record, 6 hand-made open record of the same client, 7 create.
Only steps 1–4 can adopt automatically; 5–6 (and 3–4 with ENGINE_ADOPTION=confirm_all) give candidates.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from quotes.models import GAQL_MODULE

from . import flags, registry, schema, zoho
from .facts import Facts
from .limits import Budget
from .models import ServiceLink
from .registry import CLOSED_STAGE_PATTERN, COPY_RULE_IDS, KEY_FIELD, ServiceSpec

logger = logging.getLogger(__name__)

WEB_QUOTE_REQUEST = 'Web Quote Request'
PHONE_CALL_RECEIVED = 'Phone Call Received'
COPY_WINDOW_S = 120
SEARCH_PER_PAGE = 20
DONE_STATES = ('created', 'adopted')


@dataclass
class Existing:
    """Step 1: the link's own record; `deleted` when Zoho answered 204/404."""
    record_id: str
    deleted: bool


@dataclass
class Adoption:
    record_id: str
    origin: str                          # adopted_key | adopted_copy
    record: dict | None = None           # the record when the step already read it
    duplicates: list[str] = field(default_factory=list)   # later workflow copies (a "safe to delete" Note each)


@dataclass
class Candidates:
    items: list[dict]                    # [{module, id, name, stage, created, url, match}]


# --- Helpers ----------------------------------------------------------------------------------------

def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else timezone.make_aware(parsed, timezone.utc)


def created_field(module: str) -> str:
    """`Created_Time`, or `Modified_Time` where the metadata does not show Created_Time (Translation [F3])."""
    fields = schema.get_fields(module)
    if fields is not None and 'Created_Time' not in fields:
        return 'Modified_Time'
    return 'Created_Time'


def _candidate(spec: ServiceSpec, record: dict, match: str, created_name: str) -> dict:
    record_id = str(record.get('id'))
    return {
        'module': spec.module, 'id': record_id, 'name': str(record.get(spec.name_field) or ''),
        'stage': str(record.get(spec.stage_field) or ''), 'created': str(record.get(created_name) or ''),
        'url': spec.url(record_id), 'match': match,
    }


def _linked_elsewhere(link, module: str, record_ids: list[str]) -> set[str]:
    return set(ServiceLink.objects.filter(target_module=module, target_record_id__in=record_ids,
                                          state__in=DONE_STATES).exclude(pk=link.pk)
               .values_list('target_record_id', flat=True))


def _client_criteria(spec: ServiceSpec, facts: Facts) -> str | None:
    """Zoho search criteria for this client's records: email (I-9: phone)."""
    if spec.search_by == 'phone':
        values = [v for v in (facts.client.get('phone_e164'), facts.client.get('phone_raw')) if v]
        names = [spec.client.phone] if spec.client.phone else []
    else:
        values = [facts.client.get('email')] if facts.client.get('email') else []
        names = list(spec.client.emails)
    terms = [f'({name}:equals:{zoho.criteria_value(value)})' for name in names for value in dict.fromkeys(values)]
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else f'({"or".join(terms)})'


def _search_client(spec: ServiceSpec, facts: Facts, budget: Budget) -> list[dict]:
    criteria = _client_criteria(spec, facts)
    if criteria is None:
        return []
    return zoho.search(spec.module, criteria, per_page=SEARCH_PER_PAGE, timeout=budget.timeout())


# --- Step 3: exact workflow copy -------------------------------------------------------------------

def workflow_copies(events: list[dict], module: str) -> list[tuple[datetime | None, str]]:
    """(time, record id) of every record a workflow created in `module`, earliest first."""
    copies = []
    for event in events:
        details = (event.get('automation_details') or {}).get('create_record') or {}
        if event.get('action') == 'create_record' and event.get('source') == 'workflow' \
                and (details.get('module') or {}).get('name') == module and details.get('id'):
            copies.append((parse_time(event.get('audited_time')), str(details['id'])))
    return sorted(copies, key=lambda c: (c[0] is None, c[0] or timezone.now()))


def _skip_timeline(source) -> bool:
    """A v5 quote still in the `Web Quote Request` bucket has no copy (no rule matches that value)."""
    return source.kind == 'quote' and (source.record or {}).get('GET_A_QUOTE_LEADS') == WEB_QUOTE_REQUEST


def _gaql_event_times(events: list[dict] | None, source) -> list[datetime]:
    """When the GAQL record was added or updated: from its timeline, else from the record itself."""
    if events is not None:
        times = [parse_time(e.get('audited_time')) for e in events if e.get('action') in ('added', 'updated')]
    else:
        record = source.record or {}
        times = [parse_time(record.get('Created_Time')), parse_time(record.get('Modified_Time'))]
    return [t for t in times if t is not None]


# --- Step 4: strong heuristic ----------------------------------------------------------------------

def _looks_like_copy(spec: ServiceSpec, record: dict) -> bool:
    return (record.get(spec.name_field) == spec.copy_label and record.get(spec.stage_field) == spec.intake_stage()
            and not record.get(KEY_FIELD))


def _workflow_added(events: list[dict]) -> datetime | None:
    """Time of the target's `added` event when a copy rule made it, else None."""
    for event in events:
        rule = (event.get('automation_details') or {}).get('rule') or {}
        if event.get('action') == 'added' and event.get('source') == 'workflow' and str(rule.get('id')) in COPY_RULE_IDS:
            return parse_time(event.get('audited_time')) or timezone.now()
    return None


def _confirmed_copy(spec: ServiceSpec, record: dict, gaql_times: list[datetime], budget: Budget) -> bool:
    """Adopt only when the target timeline shows a copy rule and the copy followed a GAQL event by ≤120 s."""
    added = _workflow_added(zoho.timeline(spec.module, str(record['id']), max_pages=1, timeout=budget.timeout()))
    if added is None:
        return False
    created = parse_time(record.get('Created_Time')) if created_field(spec.module) == 'Created_Time' else None
    created = created or added
    return any(0 <= (created - moment).total_seconds() <= COPY_WINDOW_S for moment in gaql_times)


@dataclass
class Heuristic:
    found: list[dict]               # this client's records in the target module
    copy_like: list[dict]           # those that look like an unlinked workflow copy
    confirmed: dict | None          # the one copy the target timeline proves (adoptable)


def strong_heuristic(link, spec: ServiceSpec, facts: Facts, source, events: list[dict] | None,
                     budget: Budget) -> Heuristic:
    """Step 4 (GETs only); also run read-only by the S1 shadow (`quotes/shadow.py`)."""
    found = _search_client(spec, facts, budget)
    copy_like = [record for record in found if _looks_like_copy(spec, record)
                 and str(record.get('id')) not in _linked_elsewhere(link, spec.module, [str(record.get('id'))])]
    gaql_times = _gaql_event_times(events, source)
    confirmed = (copy_like[0] if len(copy_like) == 1 and _confirmed_copy(spec, copy_like[0], gaql_times, budget)
                 else None)
    return Heuristic(found, copy_like, confirmed)


# --- Steps 5 and 6: candidates ---------------------------------------------------------------------

def _phone_lead_candidates(link, spec: ServiceSpec, source, budget: Budget, created_name: str) -> list[dict]:
    if source.kind != 'quote' or source.quote is None:
        return []
    items = []
    for match in source.quote.phone_matches.filter(phone_record_module=spec.module).exclude(phone_record_id=''):
        record = zoho.get(spec.module, match.phone_record_id, timeout=budget.timeout())
        if record and record.get(spec.stage_field) == PHONE_CALL_RECEIVED:
            items.append(_candidate(spec, record, 'phone_lead', created_name))
    return items


def _is_open_recent(spec: ServiceSpec, record: dict, created_name: str) -> bool:
    if CLOSED_STAGE_PATTERN.search(str(record.get(spec.stage_field) or '')):
        return False
    created = parse_time(record.get(created_name))
    return created is None or created >= timezone.now() - timedelta(days=flags.intake_candidate_days())


def candidates_for(link, spec: ServiceSpec, facts: Facts, source, budget: Budget,
                   found: list[dict] | None = None) -> list[dict]:
    """Steps 5–6: phone-lead records and open records of the same client (never adopted automatically)."""
    created_name = created_field(spec.module)
    items = _phone_lead_candidates(link, spec, source, budget, created_name)
    found = _search_client(spec, facts, budget) if found is None else found
    seen = {item['id'] for item in items}
    linked = _linked_elsewhere(link, spec.module, [str(r.get('id')) for r in found]) if found else set()
    for record in found:
        record_id = str(record.get('id'))
        if record_id in seen or record.get(KEY_FIELD) == link.external_key:
            continue
        if record.get(spec.stage_field) == PHONE_CALL_RECEIVED:
            items.append(_candidate(spec, record, 'phone_lead', created_name))
        elif record_id not in linked and _is_open_recent(spec, record, created_name):
            items.append(_candidate(spec, record, 'same_client', created_name))
        seen.add(record_id)
    return items


# --- find_existing ---------------------------------------------------------------------------------

def find_existing(link, spec: ServiceSpec, facts: Facts, source, *, budget: Budget | None = None,
                  read_only: bool = False) -> Existing | Adoption | Candidates | None:
    """The first hit of §7.6 steps 1–6, or None (create). Makes GET calls only."""
    budget = budget or Budget()
    module = spec.module
    if link.state in DONE_STATES and link.target_record_id:
        record = zoho.get(module, link.target_record_id, fields=['id'], timeout=budget.timeout())
        return Existing(link.target_record_id, deleted=record is None)

    hit = zoho.search_by_key(module, link.external_key, timeout=budget.timeout())
    if hit:
        return Adoption(str(hit['id']), 'adopted_key', record=hit)

    mode = flags.adoption_mode()
    if mode == 'off':
        return None
    create_new = bool((link.overrides or {}).get('create_new'))
    found: list[dict] | None = None

    if source.module == GAQL_MODULE and source.record_id and spec.copy_label and not _skip_timeline(source):
        try:
            events = zoho.timeline(GAQL_MODULE, source.record_id, timeout=budget.timeout())
        except zoho.ZohoPermanent as exc:
            logger.info('adoption_timeline_unreadable link=%s code=%s', link.pk, exc.code)
            events = None
        copies = [record_id for _, record_id in workflow_copies(events or [], module)]
        linked = _linked_elsewhere(link, module, copies) if copies else set()
        free = [record_id for record_id in copies if record_id not in linked]
        if free:
            if mode == 'confirm_all' and not create_new:
                record = zoho.get(module, free[0], timeout=budget.timeout()) or {'id': free[0]}
                return Candidates([_candidate(spec, record, 'exact_copy', created_field(module))])
            return Adoption(free[0], 'adopted_copy', duplicates=free[1:])

        # Step 4: the timeline is unreadable, or shows no copy (it may be truncated).
        heuristic = strong_heuristic(link, spec, facts, source, events, budget)
        found = heuristic.found
        if heuristic.confirmed is not None:
            if mode == 'confirm_all' and not create_new:
                return Candidates([_candidate(spec, heuristic.confirmed, 'strong_copy', created_field(module))])
            return Adoption(str(heuristic.confirmed['id']), 'adopted_copy', record=heuristic.confirmed)
        if heuristic.copy_like and not create_new:
            return Candidates([_candidate(spec, record, 'possible_copy', created_field(module))
                               for record in heuristic.copy_like])

    if create_new:
        return None
    items = candidates_for(link, spec, facts, source, budget, found=found)
    return Candidates(items) if items else None


# --- Enrich ----------------------------------------------------------------------------------------

NEVER_ENRICHED = frozenset({'Tracking_ID', 'Owner'})


def enrich_payload(link, spec: ServiceSpec, facts: Facts, record: dict, source_kind: str) -> dict:
    """The fields of the adoption PUT: our references always, everything else only where the copy is blank."""
    desired = registry.build(spec, facts, source_kind=source_kind, key=link.external_key)
    fields: dict = {}
    if not record.get(KEY_FIELD):
        fields[KEY_FIELD] = link.external_key
    for name in ('Quote_Request', 'Quote_Reference', 'Created_From'):
        if name in desired:
            fields[name] = desired[name]
    for name in (spec.contact_lookup, 'Attribution_Record'):
        if name in desired and not record.get(name):
            fields[name] = desired[name]
    if record.get(spec.name_field) == spec.copy_label:
        fields[spec.name_field] = desired[spec.name_field]
    for name in registry.enrich_fields(spec):
        if name in desired and record.get(name) in (None, '', []):
            fields[name] = desired[name]
    comments = spec.client.comments
    if comments and comments in desired:
        existing = str(record.get(comments) or '')
        ref = facts.source.get('ref_label') or ''
        if not (existing.startswith('[Q-') or (ref and existing.startswith(f'[{ref}]'))):
            fields[comments] = f'{desired[comments]}\n\n{existing}' if existing.strip() else desired[comments]
    for name in (spec.stage_field, *NEVER_ENRICHED):
        fields.pop(name, None)
    return fields


def enrich(link, spec: ServiceSpec, facts: Facts, record: dict, *, budget: Budget | None = None,
           source_kind: str = 'quote') -> None:
    """One PUT with `trigger: []` (DESIGN §7.6); stage, Tracking_ID and Owner are never touched."""
    budget = budget or Budget()
    result = schema.guard(spec.module, enrich_payload(link, spec, facts, record, source_kind), blocking=[])
    if KEY_FIELD in result.payload:
        link.target_key_written = True
    if result.payload:
        zoho.update(spec.module, str(record['id']), result.payload, trigger=flags.trigger_for(spec.module),
                    timeout=budget.timeout())
