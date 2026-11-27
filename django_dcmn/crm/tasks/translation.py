"""Translation-checkbox signals to Zoho (DESIGN §10.3).

A captured signal waits until the legacy sync has created its record (`orders.ZohoSyncJob` synced). Then the
actual `job.zoho_module` / `job.zoho_record_id` (possibly a matched phone-lead record) gets one fill-empty PUT
with `trigger: []`, every value through the schema guard, a Note, and the rule `translation_from_form`
(`suggest` by default). The Note's title is the write-back title of the suggested link, so the engine's
write-back finds it and never posts a second one.
"""

import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

from orders.models import ZohoSyncJob
from quotes.models import GAQL_MODULE

from .. import flags, rules, schema, sources, zoho
from ..engine import external_key
from ..facts import FORM_ORDER_LABELS
from ..models import ServiceSignal
from ..registry import DEAL_LANGUAGES, TRANSLATION_REQUIRED_YES
from ..translation_capture import KIND, SERVICE_KEY
from ..writeback import note_title

logger = logging.getLogger(__name__)

LOCK_KEY = 'crm:translation:apply'
LOCK_TTL_S = 240
BATCH = 100
OPEN_STATES = ('waiting_source', 'fields_written')
ERROR_MAX = 200
TOKEN_REFRESH = 'TOKEN_REFRESH'   # crm.zoho's code for a failed OAuth refresh


class _Retry(Exception):
    """Try again on the next beat (the code is kept in `last_error`)."""


class _Failed(Exception):
    """Cannot go on: the signal ends in `error`."""


# --- What each module gets ----------------------------------------------------------------------

def _is_empty(value) -> bool:
    return value is None or value is False or (isinstance(value, str) and value.strip() in ('', '-None-'))


def _writer(flag_field: str, flag_value, *, deal_language: bool = False) -> Callable[[dict, dict], dict]:
    """(signal data, current record) → the empty fields to fill. `No - Translation` / `NO` are never written."""
    def write(data: dict, record: dict) -> dict:
        fields = {flag_field: flag_value}
        if data.get('lang_from'):
            fields['Translation_Source_Language'] = data['lang_from']
        if data.get('lang_to'):
            fields['Translation_Target_Language'] = data['lang_to']
        if deal_language and data.get('lang_to') in DEAL_LANGUAGES:
            fields['Translated_Language'] = data['lang_to']
        return {name: value for name, value in fields.items() if _is_empty(record.get(name))}
    return write


# zoho_module → fields for a fill-empty PUT. Any other module (Get_A_Quote_Leads, a merged phone lead) gets
# the Note only.
MODULE_WRITERS: dict[str, Callable[[dict, dict], dict]] = {
    'Deals': _writer('Translation_Required', TRANSLATION_REQUIRED_YES, deal_language=True),
    'Apostille_Services': _writer('Translation_Required', TRANSLATION_REQUIRED_YES),
    'Embassy_Legalization': _writer('Translation_Required', TRANSLATION_REQUIRED_YES),
    'Triple_Seal_Apostilles': _writer('Translation_Required', True),
    'FINGERPRINT_SERVICES': _writer('Translation_Needed', 'YES'),
}


def one_click_ready(module: str) -> bool:
    """Whether a button click on this record reaches the engine (engine on, module an allowed source)."""
    if flags.engine_mode() == 'off':
        return False
    try:
        sources.check_allowed(module)
    except sources.SourceNotAllowed:
        return False
    return True


def note_text(signal: ServiceSignal, *, one_click: bool) -> str:
    data = signal.data or {}
    source, target = data.get('lang_from'), data.get('lang_to')
    languages = f' ({source or "?"} → {target or "?"})' if source or target else ''
    form = FORM_ORDER_LABELS.get(signal.order_type, signal.order_type.title())
    asked = f'Client asked for a certified translation{languages} on the {form} form.'
    if not one_click:
        return f'{asked} Create it in Translation Services (one-click button coming).'
    button = 'Create Translation' if signal.source_module == GAQL_MODULE else 'Add translation'
    return f'{asked} Click {button} to create it in Translation Services.'


# --- One signal ---------------------------------------------------------------------------------

def _write_fields(signal: ServiceSignal) -> None:
    module, record_id = signal.source_module, signal.source_record_id
    writer = MODULE_WRITERS.get(module)
    if writer is None:
        return
    if schema.get_fields(module) is None:
        raise _Retry('schema_no_metadata')
    record = zoho.get(module, record_id)
    if record is None:
        raise _Failed('record_not_found')
    fields = writer(signal.data or {}, record)
    guarded = schema.guard(module, fields, blocking=()) if fields else None
    if not (guarded and guarded.payload):
        return
    try:
        zoho.update(module, record_id, guarded.payload)
    except zoho.ZohoPermanent as exc:   # the Note still tells the manager; the code stays visible
        signal.last_error = f'fields:{exc.code}'[:ERROR_MAX]
        logger.warning('translation_fields_failed signal=%s module=%s record=%s code=%s', signal.pk, module,
                       record_id, exc.code)
        return
    logger.info('translation_fields_written signal=%s module=%s record=%s fields=%s', signal.pk, module, record_id,
                ','.join(sorted(guarded.payload)))


def _post_note(signal: ServiceSignal) -> None:
    """Once per record (by title), shared with the write-back Note of the suggested link."""
    module, record_id = signal.source_module, signal.source_record_id
    title = note_title(external_key(signal.source_key, SERVICE_KEY, 1), 'suggested')
    if any(str(note.get('Note_Title') or '').startswith(title) for note in zoho.list_notes(module, record_id)):
        return
    zoho.add_note(module, record_id, title, note_text(signal, one_click=one_click_ready(module)))


def apply_signal(signal: ServiceSignal, job: ZohoSyncJob | None = None) -> str:
    """Take one signal as far as it can go; returns waiting|retry|error or the rule outcome."""
    try:
        if signal.state == 'waiting_source':
            if job is None:
                return 'waiting'
            signal.source_module, signal.source_record_id = job.zoho_module, job.zoho_record_id
            signal.last_error = ''
            _write_fields(signal)
            signal.state = 'fields_written'
            signal.save(update_fields=['source_module', 'source_record_id', 'state', 'last_error'])
        _post_note(signal)
        if flags.engine_mode() == 'off':   # the rule needs the engine: the signal waits, as intake's do
            return 'waiting'
        return rules.evaluate(signal)
    except zoho.ZohoTransient as exc:
        return _failed(signal, exc.code or 'TRANSIENT', retry=True)
    except zoho.ZohoPermanent as exc:   # a token problem is not this signal's: it tries again
        return _failed(signal, exc.code or 'PERMANENT', retry=exc.code == TOKEN_REFRESH)
    except (_Retry, _Failed) as exc:
        return _failed(signal, str(exc), retry=isinstance(exc, _Retry))


def _failed(signal: ServiceSignal, code: str, *, retry: bool) -> str:
    ServiceSignal.objects.filter(pk=signal.pk).update(
        attempts=signal.attempts + 1, last_error=code[:ERROR_MAX], **({} if retry else {'state': 'error'}))
    logger.warning('translation_apply_failed signal=%s code=%s retry=%s', signal.pk, code, retry)
    return 'retry' if retry else 'error'


# --- The beat task ------------------------------------------------------------------------------

def _synced_jobs(signals: Iterable[ServiceSignal]) -> dict[tuple[str, int], ZohoSyncJob]:
    ids: dict[str, list[int]] = defaultdict(list)
    for signal in signals:
        ids[signal.order_type].append(signal.order_id)
    jobs = {}
    for order_type, order_ids in ids.items():
        synced = (ZohoSyncJob.objects.filter(order_type=order_type, order_id__in=order_ids,
                                             status=ZohoSyncJob.STATUS_SYNCED)
                  .exclude(zoho_module='').exclude(zoho_record_id=''))
        jobs.update({(job.order_type, job.order_id): job for job in synced})
    return jobs


@shared_task(name='crm.tasks.apply_translation_signals')
def apply_translation_signals() -> str:
    """Beat, every 300 s (TRANSLATION_CAPTURE_ENABLED): write captured requests to their synced records.

    A signal still open 30 days after capture (FBI and Marriage sync only after payment) becomes `expired`.
    """
    if not flags.translation_capture_enabled():
        return 'skipped'
    owner = uuid.uuid4().hex
    if not cache.add(LOCK_KEY, owner, LOCK_TTL_S):
        return 'locked'
    try:
        expired = (ServiceSignal.objects.filter(kind=KIND, state__in=OPEN_STATES, expires_at__lt=timezone.now())
                   .update(state='expired'))
        open_signals = list(ServiceSignal.objects.filter(kind=KIND, state__in=OPEN_STATES).order_by('created_at'))
        jobs = _synced_jobs(s for s in open_signals if s.state == 'waiting_source')
        ready = [s for s in open_signals if s.state == 'fields_written' or (s.order_type, s.order_id) in jobs][:BATCH]
        outcomes: Counter = Counter()
        for signal in ready:
            try:
                outcomes[apply_signal(signal, jobs.get((signal.order_type, signal.order_id)))] += 1
            except Exception:
                logger.exception('translation_apply_error signal=%s', signal.pk)
                outcomes[_failed(signal, 'internal', retry=True)] += 1
    finally:
        if cache.get(LOCK_KEY) == owner:
            cache.delete(LOCK_KEY)
    summary = ' '.join(f'{name}={count}' for name, count in sorted(outcomes.items()))
    logger.info('translation_signals_applied expired=%s not_ready=%s %s', expired, len(open_signals) - len(ready),
                summary)
    return f'expired={expired} {summary}'.strip()
