"""Which panel may send which engine option (DESIGN §8.2 table, §8.4).

The token panel (`panel`, opened by a minted link; the `zoho` key can mint one) gets the subset that cannot
do more than a manager can already do by hand: link only a listed candidate, "create again" only after the
server re-checks that the record is gone (once per link per hour), no `force_spam`, no owner override. The
Plan B staff panel (`staff`, a personal Django login) gets every option.
"""

import re
from typing import Literal

from django.core.cache import cache

from .. import registry, zoho
from ..registry import AUTOMATED_MARK

TOKEN_PANEL_OPTIONS: frozenset[str] = frozenset({
    'link_existing', 'create_new', 'create_again', 'include_suggested', 'overrides.stage',
    'overrides.comment_append',
})
STAFF_PANEL_OPTIONS: frozenset[str] = TOKEN_PANEL_OPTIONS | {'force_spam', 'overrides.owner_id'}
PANEL_OPTIONS = {'panel': TOKEN_PANEL_OPTIONS, 'staff': STAFF_PANEL_OPTIONS}

COMMENT_MAX = 500
CREATE_AGAIN_KEY = 'crm:panel:again:{link_id}'
CREATE_AGAIN_EVERY_S = 3600
CHECK_TIMEOUT: zoho.Timeout = (3, 8)
RECORD_ID = re.compile(r'^\d{1,32}$')


class OptionNotAllowed(Exception):
    """HTTP 403 `option_not_allowed` (audited by the caller)."""

    code = 'option_not_allowed'

    def __init__(self, option: str, reason: str):
        super().__init__(f'{option}: {reason}')
        self.option = option
        self.reason = reason


def option_names(options: dict) -> list[str]:
    """`{"create_new": True, "overrides": {"stage": …}}` → `["create_new", "overrides.stage"]` (set values only)."""
    names = []
    for name, value in (options or {}).items():
        if name == 'overrides' and isinstance(value, dict):
            names += [f'overrides.{key}' for key, item in value.items() if item not in (None, '', False)]
        elif value not in (None, '', False):
            names.append(name)
    return names


def check_options(caller: Literal['panel', 'staff'], options: dict, link, *, service_key: str | None = None) -> None:
    """Raise OptionNotAllowed unless `caller` may send `options` for this link (or, with no link yet, service)."""
    allowed = PANEL_OPTIONS.get(caller, frozenset())
    names = option_names(options)
    for name in names:
        if name not in allowed:
            raise OptionNotAllowed(name, f'not available in the {caller} panel')
    overrides = options.get('overrides') or {}
    if 'overrides.stage' in names:
        _check_stage(link.service_key if link is not None else service_key, str(overrides['stage']))
    if 'overrides.comment_append' in names and len(str(overrides['comment_append'])) > COMMENT_MAX:
        raise OptionNotAllowed('overrides.comment_append', f'longer than {COMMENT_MAX} characters')
    if 'overrides.owner_id' in names and not RECORD_ID.match(str(overrides['owner_id'])):
        raise OptionNotAllowed('overrides.owner_id', 'not a Zoho user id')
    if 'link_existing' in names:
        _check_link_existing(caller, str(options['link_existing']), link)
    if 'create_again' in names and caller == 'panel':
        _check_create_again(link)


def _check_stage(service_key: str | None, stage: str) -> None:
    spec = registry.REGISTRY.get(service_key or '')
    if spec is None or stage not in spec.stages() or AUTOMATED_MARK in stage:
        raise OptionNotAllowed('overrides.stage', 'not one of the stages of this service')


def _check_link_existing(caller: str, record_id: str, link) -> None:
    if not RECORD_ID.match(record_id):
        raise OptionNotAllowed('link_existing', 'not a Zoho record id')
    if caller == 'staff':
        return
    listed = {str(candidate.get('id')) for candidate in (link.candidates if link is not None else None) or []}
    if record_id not in listed:
        raise OptionNotAllowed('link_existing', 'not one of the listed candidates')


def _check_create_again(link) -> None:
    """Token panel: only when a fresh GET of the current target answers 204/404, at most once per link per hour."""
    if link is None or not link.target_module or not link.target_record_id:
        raise OptionNotAllowed('create_again', 'there is no record to create again')
    try:
        record = zoho.get(link.target_module, link.target_record_id, fields=['id'], timeout=CHECK_TIMEOUT)
    except (zoho.ZohoTransient, zoho.ZohoPermanent):
        raise OptionNotAllowed('create_again', 'Zoho could not confirm that the record is gone') from None
    if record is not None:
        raise OptionNotAllowed('create_again', 'the record still exists')
    if not cache.add(CREATE_AGAIN_KEY.format(link_id=link.pk), 1, CREATE_AGAIN_EVERY_S):
        raise OptionNotAllowed('create_again', 'already created again in the last hour')
