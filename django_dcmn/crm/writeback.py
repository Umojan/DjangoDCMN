"""Write-back of engine state to the source and target records (DESIGN §7.9).

Recomputed from Django every time, so it converges and is safe to run twice. One owner at a time per source
(`crm:wb:{source_key}`, 60 s, released by its owner only); Notes are idempotent by their title prefix
`DCMN · {external_key} · {event}`. A v5 quote also gets one PUT of its line fields; never `Quote_Status`.
"""

import logging
import uuid
from collections.abc import Callable, Iterable

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from quotes import contract as c
from quotes import render
from quotes.models import GAQL_MODULE, QuoteSubmission

from . import flags, schema, zoho
from .alerts import alert_ops
from .facts import source_url
from .messages import label, trigger_word
from .models import ServiceLink
from .registry import NO_TARGET
from .schema_spec import SERVICE_VALUES

logger = logging.getLogger(__name__)

LOCK_KEY = 'crm:wb:{source_key}'
LOCK_TTL_S = 60
FAILURE_ALERT_AFTER = 3
DONE_STATES = ('created', 'adopted')
SUBFORM_FIELD = 'Quote_Lines'
SUBFORM_STATUS = {
    None: 'Requested', 'suggested': 'Suggested', 'dismissed': 'Dismissed', 'pending': 'Creating',
    'creating': 'Creating', 'retry': 'Creating', 'created': 'Created', 'adopted': 'Linked',
    'needs_decision': 'Needs decision', 'degraded': 'Failed', 'failed': 'Failed', 'target_deleted': 'Requested',
}
# Link state → source Note event.
SOURCE_EVENTS = {'created': 'created', 'adopted': 'linked', 'needs_decision': 'needs decision', 'failed': 'failed',
                 'suggested': 'suggested', 'degraded': 'setup changed'}


class WritebackBusy(Exception):
    """Another run holds the write-back lock of this source."""


def note_title(external_key: str, event: str) -> str:
    return f'DCMN · {external_key} · {event}'


def update_writeback(link: ServiceLink, change: Callable[[dict], dict]) -> dict:
    """Apply `change` to the stored `writeback` dict under a row lock, never to a stale in-memory copy.

    A write-back run holds its links for seconds of Zoho calls; a concurrent `engine._adopt` (duplicate_copies)
    or another part of the run may have saved keys meanwhile, and they must survive.
    """
    with transaction.atomic():
        stored = (ServiceLink.objects.select_for_update().filter(pk=link.pk)
                  .values_list('writeback', flat=True).first())
        updated = change(dict(stored or {}))
        ServiceLink.objects.filter(pk=link.pk).update(writeback=updated, updated_at=timezone.now())
    link.writeback = updated
    return updated


# --- Progress ---------------------------------------------------------------------------------------

def service_progress(links: Iterable[ServiceLink], lines) -> tuple[str, int]:
    """(Service_Progress, Lines_Pending) of a quote from its lines and their current links."""
    states = {link.service_key: link.state for link in links}
    targetable = [line.service_key for line in lines if line.service_key not in NO_TARGET]
    if not targetable:
        return 'Nothing to create', 0
    pending = sum(1 for key in targetable if states.get(key) not in ('created', 'adopted', 'dismissed'))
    active = [key for key in targetable if states.get(key) != 'dismissed']
    done = [key for key in active if states.get(key) in DONE_STATES]
    if len(done) == len(active):
        return 'All created', pending
    if not done:
        return 'Not started', pending
    return 'Partially created', pending


def current_links(source_key: str) -> dict[str, ServiceLink]:
    """The latest link (highest seq) per service of a source."""
    links: dict[str, ServiceLink] = {}
    for link in ServiceLink.objects.filter(source_key=source_key).order_by('service_key', 'seq'):
        links[link.service_key] = link
    return links


# --- Notes ------------------------------------------------------------------------------------------

def _by(link: ServiceLink) -> str:
    actor = link.actor or {}
    who = actor.get('initials') or ('DCMN' if link.caller in ('system', 'rules') else '')
    parts = [f'by {who}' if who else '', trigger_word(link.trigger)]
    return ' · '.join(part for part in parts if part)


def _ref_label(link: ServiceLink) -> str:
    if link.quote_id and link.quote:
        return link.quote.reference
    return 'the source record'


def source_note(link: ServiceLink, event: str) -> str:
    name, ref = label(link.service_key), link.request.ref if link.request else ''
    if event == 'created':
        return f'{name} → created: {link.target_url} · {_by(link)}' + (f' · {ref}' if ref else '')
    if event == 'linked':
        how = '(Zoho auto-copy)' if link.origin == 'adopted_copy' else '(existing record)'
        return f'{name} → linked {how}: {link.target_url} · {_by(link)}' + (f' · {ref}' if ref else '')
    if event == 'needs decision':
        urls = ', '.join(c.get('url') or c.get('id') or '' for c in link.candidates or [])
        if link.trigger == 'intake':
            return (f'Not created automatically: this client already has an open {name} record {urls}. '
                    'Link it or create new in Add service…')
        return f'{name} → needs a decision: {urls}' + (f' · {ref}' if ref else '')
    if event == 'failed':
        return f'{name} → failed: {link.last_error_code or "error"}' + (f' · {ref}' if ref else '')
    if event == 'setup changed':
        return f'{name} → not created, Zoho setup changed ({link.last_error})' + (f' · {ref}' if ref else '')
    return f"{name} → suggested (from the client's answers). Use Create {name} to make it."


def target_note(link: ServiceLink, source_url: str) -> str:
    ref = _ref_label(link)
    if link.state == 'adopted' and link.origin == 'adopted_copy':
        return f'Linked to quote {ref} (this was the auto-copy) {source_url}'.rstrip()
    if link.state == 'adopted':
        return f'Linked to {ref} {source_url} · {_by(link)}'.replace('  ', ' ')
    return f'Created from {"quote " if link.quote_id else ""}{ref} {source_url} · {_by(link)}'.replace('  ', ' ')


class _Notes:
    """Lists the Notes of a record once per run and posts a Note only if no title has the prefix."""

    def __init__(self):
        self._cache: dict[tuple[str, str], list[dict]] = {}

    def ensure(self, module: str, record_id: str, title: str, content: str) -> str:
        key = (module, record_id)
        if key not in self._cache:
            self._cache[key] = zoho.list_notes(module, record_id)
        existing = next((str(n['id']) for n in self._cache[key]
                         if str(n.get('Note_Title') or '').startswith(title)), '')
        if existing:
            return existing
        note_id = zoho.add_note(module, record_id, title, content)
        self._cache[key].append({'id': note_id, 'Note_Title': title})
        return note_id


def _source_record(link: ServiceLink) -> tuple[str, str, str]:
    """(module, record id, url) of the link's source record."""
    if link.source_kind == 'quote' and link.quote_id:
        return GAQL_MODULE, link.quote.zoho_record_id, link.quote.zoho_url
    return link.source_module, link.source_record_id, source_url(link.source_module, link.source_record_id) or ''


def _note_link(link: ServiceLink, notes: _Notes) -> None:
    """The source and target Notes of one link, each once per event; ids kept in `link.writeback`."""
    original = dict(link.writeback or {})
    writeback = dict(original)
    module, record_id, url = _source_record(link)
    event = SOURCE_EVENTS.get(link.state)
    if event and record_id:
        done = writeback.get('source_note') or {}
        if not (done.get('state') == 'done' and done.get('event') == event):
            note_id = notes.ensure(module, record_id, note_title(link.external_key, event), source_note(link, event))
            writeback['source_note'] = {'state': 'done', 'id': note_id, 'event': event}
    if link.state in DONE_STATES and link.target_record_id:
        done = writeback.get('target_note') or {}
        if done.get('state') != 'done':
            note_id = notes.ensure(link.target_module, link.target_record_id,
                                   note_title(link.external_key, 'source'), target_note(link, url))
            writeback['target_note'] = {'state': 'done', 'id': note_id}
    noted = list(writeback.get('duplicate_notes') or [])
    for copy_id in writeback.get('duplicate_copies') or []:
        if copy_id not in noted:
            notes.ensure(link.target_module, copy_id, note_title(link.external_key, 'duplicate copy'),
                         f'Duplicate auto-copy of {_ref_label(link)} — safe to delete')
            noted.append(copy_id)
            writeback['duplicate_notes'] = noted
    changed = {key: value for key, value in writeback.items() if original.get(key) != value}
    if changed:   # only the keys this run set: a concurrent writer's keys stay
        update_writeback(link, lambda stored: {**stored, **changed})


def _mark_failed(link: ServiceLink, part: str, code: str) -> None:
    def change(stored: dict) -> dict:
        return {**stored, part: {**(stored.get(part) or {}), 'state': 'failed', 'code': code},
                'failures': int(stored.get('failures') or 0) + 1}

    failures = update_writeback(link, change)['failures']
    if failures >= FAILURE_ALERT_AFTER:
        alert_ops('writeback_failed', link.external_key, 'Engine write-back keeps failing',
                  f'Write-back of {link.external_key} failed {failures} times (last: {part} {code}). '
                  'The target record itself is correct; the reconciler keeps retrying.')


# --- The quote's line fields -------------------------------------------------------------------------

def _line_fields(quote: QuoteSubmission, links: dict[str, ServiceLink]) -> dict:
    lines = list(quote.lines.order_by('position'))
    progress, pending = service_progress(links.values(), lines)
    done = [key for key, link in links.items() if link.state in DONE_STATES]
    fields = {
        'Quote_Lines_Text': render.lines_text(quote, links),
        'Services_Requested': [SERVICE_VALUES[line.service_key] for line in lines if line.service_key in SERVICE_VALUES],
        'Services_Created': [SERVICE_VALUES[key] for key in done if key in SERVICE_VALUES],
        'Lines_Pending': pending,
        'Service_Progress': progress,
    }
    if quote.zoho_lines_mode == 'subform':
        fields[SUBFORM_FIELD] = [_subform_row(quote, line, links.get(line.service_key)) for line in lines]
    return fields


def _subform_row(quote: QuoteSubmission, line, link: ServiceLink | None) -> dict:
    """The full row rebuilt from Django, with the Zoho row id when known (C8, ASSUMPTION A7)."""
    row = {
        'Service': c.SERVICE_DISPLAY[line.service_key], 'Service_Key': line.service_key,
        'Line_Ref': f'{quote.reference}/{line.position}', 'Details': render.line_summary(line)[:2000],
        'Price_Shown': render.line_price_text(line)[:255],
        'Line_Status': 'No pipeline' if line.service_key in NO_TARGET else SUBFORM_STATUS.get(
            link.state if link else None, 'Requested'),
    }
    if link is not None and link.target_url and link.state in DONE_STATES:
        row['Service_Record'] = link.target_url
        row['Done_By'] = _by(link)[:120]
    if line.zoho_row_id:
        row['id'] = line.zoho_row_id
    return row


def _save_row_ids(quote: QuoteSubmission) -> None:
    record = zoho.get(GAQL_MODULE, quote.zoho_record_id, fields=[SUBFORM_FIELD]) or {}
    by_key = {str(row.get('Service_Key')): str(row.get('id')) for row in record.get(SUBFORM_FIELD) or []
              if row.get('id')}
    for line in quote.lines.all():
        row_id = by_key.get(line.service_key)
        if row_id and row_id != line.zoho_row_id:
            line.zoho_row_id = row_id
            line.save(update_fields=['zoho_row_id'])


def _write_quote_fields(quote: QuoteSubmission, links: dict[str, ServiceLink]) -> None:
    result = schema.guard(GAQL_MODULE, _line_fields(quote, links), blocking=[])
    if not result.payload:
        return
    zoho.update(GAQL_MODULE, quote.zoho_record_id, result.payload, trigger=flags.trigger_for(GAQL_MODULE))
    if SUBFORM_FIELD in result.payload:
        _save_row_ids(quote)
    done = {'source_lines': {'state': 'done', 'at': timezone.now().isoformat()}}
    for link in links.values():
        update_writeback(link, lambda stored: {**stored, **done})


# --- Entry point ------------------------------------------------------------------------------------

def write_back(source_key: str) -> None:
    """Rewrite the source's line fields (v5 quotes) and post the missing Notes; raises WritebackBusy if locked."""
    lock = LOCK_KEY.format(source_key=source_key)
    owner = uuid.uuid4().hex
    if not cache.add(lock, owner, LOCK_TTL_S):
        raise WritebackBusy(source_key)
    try:
        _write_back(source_key)
    finally:
        if cache.get(lock) == owner:
            cache.delete(lock)


def _write_back(source_key: str) -> None:
    links = current_links(source_key)
    if source_key.startswith('qr:'):
        quote = QuoteSubmission.objects.filter(pk=int(source_key.split(':')[1])).first()
        if quote is None or not quote.zoho_record_id:
            return   # the GAQL record does not exist yet; the quotes pipeline writes the lines at create
        try:
            _write_quote_fields(quote, links)
        except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
            logger.warning('writeback_lines_failed source=%s code=%s', source_key, exc.code)
            for link in links.values():
                _mark_failed(link, 'source_lines', exc.code)
    notes = _Notes()
    for link in links.values():
        try:
            _note_link(link, notes)
        except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
            logger.warning('writeback_notes_failed link=%s code=%s', link.pk, exc.code)
            _mark_failed(link, 'source_note', exc.code)
    logger.info('writeback_done source=%s links=%s', source_key, len(links))


def needs_retry(link: ServiceLink) -> bool:
    """A write-back part failed; the reconciler runs the source again."""
    return any(isinstance(part, dict) and part.get('state') == 'failed' for part in (link.writeback or {}).values())
