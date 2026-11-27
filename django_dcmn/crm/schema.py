"""Zoho field metadata: the cache, the runtime guard (DESIGN §7.10) and the `zoho_schema` plan (§6.6).

The guard makes sure Django never writes a field or a picklist value Zoho does not have, and never exceeds
a field length (invariant 8). Its metadata comes from the cache (refreshed by `crm.tasks.refresh_zoho_schema`),
else from the last `ZohoSchemaSnapshot`; `get_fields` and `guard` never call Zoho themselves.

The plan compares live metadata with `crm/schema_spec.py` and makes GET requests only.
"""

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from django.core.cache import cache
from django.utils import timezone

from orders.zoho_errors import ZohoPermanentError
from quotes import flags as quote_flags

from . import schema_spec, zoho
from .models import ZohoSchemaSnapshot
from .schema_spec import FieldSpec

logger = logging.getLogger(__name__)

CACHE_KEY = 'crm:schema:{module}'
CACHE_TTL_S = 6 * 3600
REFRESH_MODULES = tuple(schema_spec.SPEC)

ELLIPSIS = '…'
CUT_TYPES = frozenset({'text', 'textarea'})                 # over the length: cut with an ellipsis
STRICT_LENGTH_TYPES = frozenset({'email', 'phone', 'website'})  # over the length: dropped (a cut value is invalid)
INTEGER_TYPES = frozenset({'integer', 'bigint'})
NUMBER_TYPES = frozenset({'double', 'currency', 'decimal', 'percent'})
NEVER_CUT = frozenset({'External_Order_Key', 'Name'})       # built to fit; dropped rather than cut
DROPPED_VALUE_MAX = 200

# Plan actions.
ADD, OK, MANUAL, MISMATCH, BLOCKED = 'ADD', 'OK', 'MANUAL', 'MISMATCH', 'BLOCKED'
NONE_VALUE = '-None-'   # Zoho's empty picklist option


# --- Metadata cache -----------------------------------------------------------------------------

def normalise(raw_fields: Iterable[dict]) -> list[dict]:
    """The snapshot form (DESIGN §4.4): active fields only, active display values only, sorted by api_name."""
    fields = []
    for raw in raw_fields:
        if raw.get('type') == 'unused':   # "Mark as unused" (ZOHO-SETUP rollback): the guard must drop it
            continue
        fields.append({
            'api_name': raw['api_name'],
            'data_type': raw.get('data_type'),
            'length': raw.get('length'),
            'read_only': bool(raw.get('read_only')),
            'pick_list_values': _active_values(raw),
        })
    return sorted(fields, key=lambda f: f['api_name'])


def _active_values(raw: dict) -> list[str]:
    return [
        str(option.get('display_value'))
        for option in raw.get('pick_list_values') or []
        if option.get('type') != 'unused'
    ]


def by_api_name(fields: Iterable[dict]) -> dict[str, dict]:
    return {f['api_name']: f for f in fields}


def get_fields(module: str) -> dict[str, dict] | None:
    """Metadata of a module by api_name: the cache (6 h), else the latest snapshot, else None."""
    fields = cache.get(CACHE_KEY.format(module=module))
    if fields is not None:
        return fields
    snapshot = ZohoSchemaSnapshot.objects.filter(module=module).order_by('-fetched_at', '-pk').first()
    return by_api_name(snapshot.fields) if snapshot else None


def refresh(module: str) -> bool:
    """Read the module's metadata from Zoho into the cache; save a snapshot if it changed (True).

    Zoho errors propagate (ZohoTransientError / ZohoPermanentError); the cache and snapshots stay as they were.
    """
    fields = normalise(zoho.fields_metadata(module))
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    cache.set(CACHE_KEY.format(module=module), by_api_name(fields), CACHE_TTL_S)
    latest = ZohoSchemaSnapshot.objects.filter(module=module).order_by('-fetched_at', '-pk').first()
    if latest and latest.sha256 == digest:
        return False
    ZohoSchemaSnapshot.objects.create(module=module, fetched_at=timezone.now(), fields=fields, sha256=digest)
    logger.info('zoho_schema_snapshot_saved module=%s fields=%s', module, len(fields))
    return True


def validate_registry(metadata: Mapping[str, dict] | None = None) -> list[str]:
    """Problems found by `crm.registry.validate` (DESIGN §7.4); [] while the registry does not exist yet (WP5).

    `metadata` maps module → fields by api_name; by default the cached (or snapshot) metadata.
    """
    try:
        registry = importlib.import_module(f'{__package__}.registry')
    except ModuleNotFoundError as exc:
        if exc.name != f'{__package__}.registry':
            raise
        return []
    if metadata is None:
        metadata = {module: fields for module in REFRESH_MODULES if (fields := get_fields(module)) is not None}
    return list(registry.validate(metadata))


# --- Runtime guard ------------------------------------------------------------------------------

@dataclass
class GuardResult:
    payload: dict                                                     # what may be written
    dropped: list[tuple[str, str]] = field(default_factory=list)     # (api_name, value as text) not written
    blocked: list[str] = field(default_factory=list)                  # blocking fields that could not be written


def guard(module: str, payload: dict, blocking: Iterable[str]) -> GuardResult:
    """Drop what Zoho would not accept; any dropped field in `blocking` makes the write `blocked`.

    Per field: unknown api_name → dropped, unless the spec gives it a text fallback that Zoho has (A2: the
    values are written there comma-joined); picklist value not active → dropped (multi-select: per value);
    text over the length → cut with an ellipsis (`External_Order_Key` and `Name` are never cut: dropped);
    email, phone or URL over the length → dropped; boolean, integer or number of the wrong type → dropped.
    With no metadata at all nothing is written and every blocking field is blocked.
    """
    blocking = list(dict.fromkeys(blocking))
    fields = get_fields(module)
    if fields is None:
        logger.warning('schema_guard_no_metadata module=%s', module)
        return GuardResult(payload={}, dropped=[(name, _as_text(value)) for name, value in payload.items()],
                           blocked=blocking)

    result = GuardResult(payload={})
    rejected = set()
    for name, value in payload.items():
        meta = fields.get(name)
        if meta is None:
            name, value, meta = _text_fallback(module, name, value, fields)
        keep, value_out, dropped_values = (False, None, [value]) if meta is None else _fit(name, value, meta)
        result.dropped.extend((name, _as_text(v)) for v in dropped_values)
        if keep:
            result.payload[name] = value_out
        else:
            rejected.add(name)
    result.blocked = [name for name in blocking if name in rejected]
    if result.dropped:
        logger.info('schema_guard_dropped module=%s fields=%s', module,
                    ','.join(sorted({name for name, _ in result.dropped})))
    if result.blocked:
        logger.warning('schema_guard_blocked module=%s fields=%s', module, ','.join(result.blocked))
    return result


def _text_fallback(module: str, name: str, value, fields: dict[str, dict]) -> tuple[str, object, dict | None]:
    """A multi-select replaced by its spec fallback (e.g. Services_Requested_Text): (name, value, meta) to write."""
    spec = schema_spec.find(module, name)
    twin = spec.fallback if spec else None
    if twin is None or twin.api_name not in fields or not isinstance(value, (list, tuple)):
        return name, value, None
    return twin.api_name, ', '.join(str(v) for v in value), fields[twin.api_name]


def _fit(name: str, value, meta: dict) -> tuple[bool, object, list]:
    """(keep, value to write, dropped values) for one known field. None (clear the field) always passes."""
    if value is None:
        return True, None, []
    data_type = meta.get('data_type')
    allowed = meta.get('pick_list_values') or []
    if data_type == 'picklist':
        valid = isinstance(value, str) and value in allowed
    elif data_type == 'multiselectpicklist':
        if not isinstance(value, (list, tuple)):
            return False, None, [value]
        kept = [v for v in value if v in allowed]
        off_list = [v for v in value if v not in allowed]
        return bool(kept) or not value, kept, off_list
    elif data_type == 'boolean':
        valid = isinstance(value, bool)
    elif data_type in INTEGER_TYPES:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif data_type in NUMBER_TYPES:
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        length = meta.get('length')
        if not (isinstance(value, str) and length and len(value) > length):
            return True, value, []
        if data_type in CUT_TYPES and name not in NEVER_CUT:
            return True, value[:length - 1] + ELLIPSIS, []
        valid = data_type not in CUT_TYPES and data_type not in STRICT_LENGTH_TYPES
    return (True, value, []) if valid else (False, None, [value])


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = ', '.join(str(v) for v in value)
    elif isinstance(value, bool):
        value = 'true' if value else 'false'
    text = '' if value is None else str(value)
    return text if len(text) <= DROPPED_VALUE_MAX else text[:DROPPED_VALUE_MAX - 1] + ELLIPSIS


# --- Plan (zoho_schema) -------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanItem:
    module: str
    api_name: str
    action: str                 # ADD | OK | MANUAL | MISMATCH | BLOCKED
    detail: str = ''
    kind: str = 'field'         # field | option | related_list | column
    step: str = ''              # ZOHO-SETUP step id of a MANUAL item
    optional: bool = False      # a MANUAL item --verify does not require (the subform while QUOTE_V3_SUBFORM=off)

    def line(self) -> str:
        text = f'{self.action} {self.api_name}' + (f' {self.detail}' if self.detail else '')
        if self.step and self.action == MANUAL:
            text += f' (ZOHO-SETUP {self.step}' + (', not required yet)' if self.optional else ')')
        return text


def fetch_metadata(modules: Iterable[str]) -> dict[str, list[dict]]:
    """Raw field metadata per module (GET only), plus the column metadata of every existing spec subform."""
    metadata: dict[str, list[dict]] = {}
    for module in modules:
        metadata[module] = zoho.fields_metadata(module)
        live = {f.get('api_name'): f for f in metadata[module]}
        for spec in schema_spec.SPEC.get(module, ()):
            if spec.data_type == 'subform' and spec.api_name in live:
                subform_module = subform_module_name(live[spec.api_name])
                try:
                    metadata[subform_module] = zoho.fields_metadata(subform_module)
                except ZohoPermanentError as exc:
                    logger.warning('zoho_schema_subform_unreadable module=%s code=%s', subform_module,
                                   getattr(exc, 'code', ''))
    return metadata


def plan(modules: list[str] | None = None) -> list[PlanItem]:
    """Read the live metadata and compare it with the spec."""
    modules = modules or list(schema_spec.SPEC)
    return plan_against(fetch_metadata(modules), modules)


def plan_against(metadata: Mapping[str, list[dict]], modules: Iterable[str] | None = None) -> list[PlanItem]:
    """One item per spec field, picklist option, related-list title and subform column (no I/O)."""
    items: list[PlanItem] = []
    for module in modules or schema_spec.SPEC:
        live_fields = list(metadata.get(module) or [])
        live = {f.get('api_name'): f for f in live_fields}
        lookups = sum(1 for f in live_fields if _is_custom_lookup(f))
        for spec in schema_spec.SPEC[module]:
            current = live.get(spec.api_name)
            if current is None:
                item = _missing_item(module, spec, live_fields, lookups)
                if item.action == ADD and spec.data_type == 'lookup':
                    lookups += 1
                items.append(item)
            else:
                problem = compare(spec, current)
                items.append(PlanItem(module, spec.api_name, MISMATCH if problem else OK, problem or describe(spec)))
                if spec.columns and not problem:
                    items.extend(_column_items(module, spec, metadata.get(subform_module_name(current))))
                for option in schema_spec.MANUAL_OPTIONS.get((module, spec.api_name), ()):
                    seen = option in _active_values(current)
                    items.append(PlanItem(module, spec.api_name, OK if seen else MANUAL, f'option {option}',
                                          kind='option', step='' if seen else spec.manual or ''))
            if spec.related_list_title:
                items.append(_related_list_item(module, spec, current))
    return items


def _missing_item(module: str, spec: FieldSpec, live_fields: list[dict], lookups: int) -> PlanItem:
    if spec.existing:
        return PlanItem(module, spec.api_name, MISMATCH, 'expected existing field is missing')
    if spec.manual:
        optional = spec.data_type == 'subform' and quote_flags.subform_mode() != 'auto'
        return PlanItem(module, spec.api_name, MANUAL, f'{describe(spec)}: create in the UI', step=spec.manual,
                        optional=optional)
    clash = next((f.get('api_name') for f in live_fields
                  if str(f.get('field_label') or '').casefold() == spec.label.casefold()
                  or str(f.get('api_name') or '').casefold() == spec.api_name.casefold()), None)
    if clash:
        return PlanItem(module, spec.api_name, BLOCKED, f'name clash with existing field {clash}')
    if spec.data_type == 'lookup' and lookups >= schema_spec.MAX_CUSTOM_LOOKUPS:
        return PlanItem(module, spec.api_name, BLOCKED,
                        f'would be custom lookup #{lookups + 1}; '
                        f'more than {schema_spec.MAX_CUSTOM_LOOKUPS} is unproven')
    return PlanItem(module, spec.api_name, ADD, describe(spec))


def _related_list_item(module: str, spec: FieldSpec, current: dict | None) -> PlanItem:
    title = spec.related_list_title
    detail = f"related list title '{title}'"
    lookup = (current or {}).get('lookup') or {}
    if current is not None and 'display_label' not in lookup:
        return PlanItem(module, spec.api_name, MANUAL, f'{detail}: not readable, check by hand', kind='related_list',
                        step=schema_spec.RELATED_LIST_STEP, optional=True)
    if lookup.get('display_label') == title:
        return PlanItem(module, spec.api_name, OK, detail, kind='related_list')
    return PlanItem(module, spec.api_name, MANUAL, detail, kind='related_list', step=schema_spec.RELATED_LIST_STEP)


def _column_items(module: str, spec: FieldSpec, columns: list[dict] | None) -> list[PlanItem]:
    optional = quote_flags.subform_mode() != 'auto'
    if columns is None:
        return [PlanItem(module, spec.api_name, MANUAL, 'columns not readable', kind='column', step=spec.manual or '',
                         optional=optional)]
    live = {f.get('api_name'): f for f in columns}
    items = []
    for column in spec.columns:
        name = f'{spec.api_name}.{column.api_name}'
        current = live.get(column.api_name)
        if current is None:
            items.append(PlanItem(module, name, MANUAL, f'{describe(column)}: add the column in the UI', kind='column',
                                  step=spec.manual or '', optional=optional))
        else:
            problem = compare(column, current)
            items.append(PlanItem(module, name, MISMATCH if problem else OK, problem or describe(column),
                                  kind='column'))
    return items


def compare(spec: FieldSpec, current: dict) -> str:
    """'' when the live field satisfies the spec, else what differs (never auto-fixed).

    Every spec value must be an active display value; extra live values (managers may add some) are fine.
    """
    problems = []
    if current.get('type') == 'unused':
        problems.append('marked unused in Zoho')
    if current.get('data_type') != spec.data_type:
        problems.append(f"type {current.get('data_type')} (spec {spec.data_type})")
    if spec.length and current.get('length') != spec.length:
        problems.append(f"length {current.get('length')} (spec {spec.length})")
    if spec.values:
        active = _active_values(current)
        missing = [v for v in spec.values if v not in active]
        if missing:
            problems.append('values missing: ' + ', '.join(missing))
    if spec.lookup_module:
        module = ((current.get('lookup') or {}).get('module') or {}).get('api_name')
        if module != spec.lookup_module:
            problems.append(f'lookup module {module} (spec {spec.lookup_module})')
    return '; '.join(problems)


def describe(spec: FieldSpec) -> str:
    if spec.data_type == 'lookup':
        return f'lookup → {spec.lookup_module}'
    if spec.values:
        return f'{spec.data_type} ({len(spec.values)} values)'
    if spec.data_type == 'textarea':
        return 'textarea small'
    if spec.length and spec.data_type == 'text':
        return f'text {spec.length}'
    return spec.data_type


def verify_failures(items: Iterable[PlanItem]) -> list[PlanItem]:
    """Items that make `--verify` fail: anything not OK, except a MANUAL item that is not required yet."""
    return [item for item in items if item.action != OK and not (item.action == MANUAL and item.optional)]


def subform_module_name(current: dict) -> str:
    """The module whose fields are the subform's columns (`associated_module`), else the field's api_name."""
    associated = current.get('associated_module')
    name = None
    if isinstance(associated, dict):
        name = associated.get('api_name') or associated.get('module')
        if isinstance(name, dict):
            name = name.get('api_name')
    return str(name or current.get('api_name'))


def _is_custom_lookup(current: dict) -> bool:
    """A custom lookup counts towards the limit even when marked unused (the conservative reading)."""
    return current.get('data_type') == 'lookup' and bool(current.get('custom_field'))
