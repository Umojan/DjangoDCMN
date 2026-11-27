"""Create rate limits and the inline semaphore (DESIGN §7.8 steps 3 and 7), counted in the shared cache."""

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from django.core.cache import cache

from . import flags, zoho
from .alerts import alert_ops

logger = logging.getLogger(__name__)

HOUR_S = 3600
EXEMPT_CALLERS = frozenset({'system'})   # intake is bounded by QUOTES_HOURLY_CEILING
INLINE_KEY = 'crm:inline'
INLINE_TTL_S = 8


def _bucket() -> int:
    return int(time.time() // HOUR_S)


def _keys(source_key: str) -> tuple[str, str]:
    bucket = _bucket()
    return f'crm:creates:{source_key}:{bucket}', f'crm:creates:all:{bucket}'


def check_create_limit(source_key: str, caller: str) -> bool:
    """True when one more real create is allowed; False alerts `engine_rate_limited`."""
    if caller in EXEMPT_CALLERS:
        return True
    per_source_key, global_key = _keys(source_key)
    per_source, total = cache.get(per_source_key, 0), cache.get(global_key, 0)
    if per_source < flags.create_limit_per_source_hour() and total < flags.create_limit_global_hour():
        return True
    logger.warning('engine_rate_limited source=%s per_source=%s total=%s', source_key, per_source, total)
    alert_ops('engine_rate_limited', 'creates',
              'Engine create rate limit reached',
              f'Creates in the last hour: {per_source} for {source_key}, {total} in total '
              f'(limits {flags.create_limit_per_source_hour()} per source, {flags.create_limit_global_hour()} in all). '
              'Further creates are refused until the hour turns. Check EngineRequest rows by caller_key_id.')
    return False


def count_create(source_key: str, caller: str) -> None:
    """Count one real create (not a preview, an `exists` answer or an adoption)."""
    if caller in EXEMPT_CALLERS:
        return
    for key in _keys(source_key):
        if not cache.add(key, 1, HOUR_S + 60):
            try:
                cache.incr(key)
            except ValueError:   # expired between add and incr
                cache.add(key, 1, HOUR_S + 60)


class BudgetExhausted(zoho.ZohoTransient):
    """Less than 2.5 s of the inline budget is left: no call is started, the link goes async."""


class Budget:
    """Per-call timeouts of one engine run: the 6 s inline budget, or the default timeout without a deadline."""

    def __init__(self, deadline: float | None = None):
        self.deadline = deadline

    def timeout(self) -> zoho.Timeout:
        timeout = zoho.budget_timeout(self.deadline)
        if timeout is None:
            raise BudgetExhausted('inline budget exhausted', code='BUDGET')
        return timeout


@contextmanager
def inline_slot() -> Iterator[bool]:
    """At most one inline engine call at a time across workers; yields False when the slot is busy."""
    owner = uuid.uuid4().hex
    acquired = cache.add(INLINE_KEY, owner, INLINE_TTL_S)
    try:
        yield acquired
    finally:
        if acquired and cache.get(INLINE_KEY) == owner:
            cache.delete(INLINE_KEY)
