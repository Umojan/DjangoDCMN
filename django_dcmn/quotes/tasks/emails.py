"""Staff and client emails of a submission (DESIGN §5.6)."""

from celery import shared_task

from crm.alerts import check_beat

from .. import emails
from ..models import QuoteSubmission


@shared_task(name='quotes.tasks.send_quote_staff_email')
def send_quote_staff_email(pk: int) -> str:
    """Staff email in the legacy daily thread; also the cheap beat-heartbeat check (DESIGN §12.2)."""
    check_beat()
    quote = QuoteSubmission.objects.filter(pk=pk).first()
    if quote is None:
        return 'missing'
    emails.send_staff_email(quote)
    return quote.staff_email_status


@shared_task(name='quotes.tasks.send_quote_confirmation_email')
def send_quote_confirmation_email(pk: int) -> str:
    """Client confirmation (QUOTE_V3_CONFIRMATION_EMAIL, off by default)."""
    quote = QuoteSubmission.objects.filter(pk=pk).first()
    if quote is None:
        return 'missing'
    emails.send_confirmation(quote)
    return quote.confirmation_email_status
