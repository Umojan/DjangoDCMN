"""Write-back of engine state to the source record (DESIGN §7.9)."""

import logging

from celery import shared_task

from .. import flags
from ..writeback import LOCK_TTL_S, WritebackBusy, write_back

logger = logging.getLogger(__name__)

# A busy lock re-queues the call: 5 s, then once more after the holder's lock must have ended. By then either
# this call holds the lock, or a later holder does, and a later holder read the links after this call's change.
REQUEUE_COUNTDOWNS_S = (5, LOCK_TTL_S + 5)
REQUEUE_COUNTDOWN_S = REQUEUE_COUNTDOWNS_S[0]


@shared_task(name='crm.tasks.writeback_source')
def writeback_source(source_key: str, requeued: int = 0) -> str:
    """Rewrite Quote_Lines_Text / Service_Progress and post the Notes of one source.

    A call that finds the lock held re-queues itself (REQUEUE_COUNTDOWNS_S); `requeued` counts the re-queues
    (an old `True` reads as 1). Nothing is written to Zoho unless the engine is live.
    """
    if flags.engine_mode() != 'live':
        return 'skipped'
    try:
        write_back(source_key)
    except WritebackBusy:
        attempt = int(requeued or 0)
        if attempt >= len(REQUEUE_COUNTDOWNS_S):
            logger.info('writeback_busy_covered source=%s', source_key)
            return 'busy'
        writeback_source.apply_async((source_key,), {'requeued': attempt + 1},
                                     countdown=REQUEUE_COUNTDOWNS_S[attempt])
        return 'requeued'
    return 'done'
