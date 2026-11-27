"""What the manager reads (DESIGN §7.14): one sentence per outcome; Deluge shows `message` as is."""

from datetime import datetime

from django.utils import timezone

from quotes import contract as c
from quotes.render import ET

from .registry import REGISTRY

DISABLED = 'One-click creation is switched off or not connected. Create the record by hand as before.'
TEST_ONLY = 'One-click is in test mode: it works only on ZZ TEST records right now.'
IN_PROGRESS = 'Already being created. Refresh the page in a few seconds.'
RATE_LIMITED = ('Too many records were created from DCMN in the last hour, so this one was paused. '
                'Nothing was created. Vlad has been alerted.')
STAR_NOT_ALLOWED = 'Create all works on web quotes only. Use the button for the service you need.'
ZOHO_UNAVAILABLE = 'Zoho is not answering. Nothing was created. Try again in a minute.'
SOURCE_NOT_ALLOWED = 'One-click does not work on this kind of record yet. Create the record by hand as before.'
SOURCE_NOT_FOUND = 'This record was not found in Zoho. Refresh the page and try again.'
UNKNOWN_SERVICE = 'Unknown service: {key}. Use Add service… to pick one.'
REFUSED_SPAM = 'This quote is marked Spam. Change Quote Status first, or use Add service…'
NOTHING_TO_DO = 'Nothing to create: every service of this quote is already done.'

TRIGGER_WORDS = {'zoho_button': 'button', 'create_all': 'Create all', 'intake': 'intake', 'panel': 'panel',
                 'staff': 'staff panel', 'gmail': 'Gmail'}
DONE = ('created', 'adopted', 'exists')


def label(service: str) -> str:
    spec = REGISTRY.get(service)
    return spec.label if spec else c.SERVICE_DISPLAY.get(service, service)


def module_label(result: dict) -> str:
    spec = REGISTRY.get(result.get('service', ''))
    return spec.module_label if spec else str(result.get('module') or 'Zoho').replace('_', ' ')


def trigger_word(trigger: str) -> str:
    return 'rule' if trigger.startswith('rule:') else TRIGGER_WORDS.get(trigger, trigger or 'DCMN')


def _day(value) -> str:
    moment = value if isinstance(value, datetime) else _parse(value)
    local = (moment or timezone.now()).astimezone(ET)
    return f'{local.day} {local:%b}'


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _age(value) -> str:
    created = _parse(value)
    if created is None:
        return 'age unknown'
    days = max((timezone.now() - created).days, 0)
    if days == 0:
        return 'less than a day old'
    return f'{days} day{"" if days == 1 else "s"} old'


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f'{", ".join(names[:-1])} and {names[-1]}'


def _one(result: dict, *, mode: str, ref: str, ref_label: str) -> str:
    status, name = result.get('status'), label(result.get('service', ''))
    on = 'this quote' if ref_label.startswith('Q-') else 'this record'
    if status == 'created':
        return f'{name} created in {module_label(result)}. Opening it.'
    if status == 'adopted':
        if result.get('origin') == 'adopted_copy':
            return (f"Zoho's auto-copy already made this {name} record. Linked it to {ref_label} "
                    'and filled the missing details. Opening it.')
        return f'{name} was already in {module_label(result)}. Linked it to {ref_label}. Opening it.'
    if status == 'exists':
        return (f'{name} already exists for {on} (created {_day(result.get("created_at"))} by '
                f'{trigger_word(result.get("trigger") or "")}). Opening it.')
    if status == 'needs_decision':
        candidates = result.get('candidates') or []
        if len(candidates) == 1:
            first = candidates[0]
            return (f'This client already has an open {name} record (stage {first.get("stage") or "unknown"}, '
                    f'{_age(first.get("created"))}). Nothing was created. '
                    'Use Add service… to link it or create a new one.')
        return (f'This client already has {len(candidates)} open {name} records. Nothing was created. '
                'Use Add service… to link one or create a new one.')
    if status == 'suggested':
        return f"Suggested only (from the client's answers). Click Create {name} to make it."
    if status == 'no_target':
        return f'{name} has no pipeline yet. Handle it from {on}, or use Add service… to pick one.'
    if status == 'refused_spam':
        return REFUSED_SPAM
    if status == 'target_deleted':
        return f'The {name} record linked to {on} was deleted. Use Add service… to create it again.'
    if status == 'degraded':
        spec = REGISTRY.get(result.get('service', ''))
        what = 'stage list' if spec and result.get('error_field') == spec.stage_field else 'fields'
        return (f'Zoho setup changed ({name} {what}). Nothing was created. Vlad has been alerted; '
                f'create it by hand for now. Ref {ref}.')
    if status == 'failed':
        code = result.get('error_code') or 'an error'
        field = f' ({result["error_field"]})' if result.get('error_field') else ''
        return f'{name} could not be created: Zoho said {code}{field}. Nothing was duplicated. Tell Vlad, ref {ref}.'
    if status in ('dry_run', 'preview'):
        prefix = 'Preview only (one-click is in test mode)' if status == 'dry_run' else 'Preview'
        if result.get('action') == 'adopt':
            return f'{prefix}: would link the existing {name} record. Nothing was written.'
        if result.get('action') == 'needs_decision':
            return f'{prefix}: this client already has an open {name} record; a manager would decide. ' \
                   'Nothing was written.'
        stage = f' at {result["stage"]}' if result.get('stage') else ''
        return f'{prefix}: would create {name} in {module_label(result)}{stage}. Nothing was written.'
    if status == 'in_progress':
        return IN_PROGRESS
    return f'{name}: {status}.'


def message_for(results: list[dict], *, mode: str, ref: str = '', ref_label: str = '') -> str:
    """The `message` of an engine answer: queued services first, else one sentence per service."""
    if not results:
        return NOTHING_TO_DO
    queued = [label(r['service']) for r in results if r.get('status') == 'queued']
    if queued:
        done = [label(r['service']) for r in results if r.get('status') in DONE]
        text = f'Creating {_join(queued)}. The quote updates in about 15 seconds.'
        return f'{text} Already done: {_join(done)}.' if done else text
    texts = list(dict.fromkeys(_one(r, mode=mode, ref=ref, ref_label=ref_label) for r in results))
    return ' '.join(texts)
