"""Step runner, reconciler and shadow tasks (DESIGN §5.2, §13.2 S1)."""

from celery import shared_task

from .. import flags
from ..pipeline import reconcile, run
from ..shadow import shadow_map


@shared_task(name='quotes.tasks.process_quote_submission', acks_late=True)
def process_quote_submission(pk: int) -> str:
    """Run the Zoho steps of one submission; retries come from the ladder and the reconciler."""
    return run(pk)


@shared_task(name='quotes.tasks.reconcile_quote_submissions')
def reconcile_quote_submissions() -> str:
    """Beat, every 120 s: republish due rows, re-arm paused rows, email watchdog, 30-min alert."""
    return ' '.join(f'{name}={count}' for name, count in reconcile().items())


@shared_task(name='quotes.tasks.shadow_map_legacy_quote')
def shadow_map_legacy_quote(quote_request_id: int) -> str:
    """S1 shadow mapping of a legacy QuoteRequest: Zoho GETs only (QUOTE_V3_SHADOW_LEGACY)."""
    if not flags.shadow_legacy():
        return 'skipped'
    return 'error' if shadow_map(quote_request_id).error else 'done'
