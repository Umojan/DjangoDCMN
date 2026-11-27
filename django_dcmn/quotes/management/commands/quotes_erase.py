"""Erase the PII of a v3 quote, or of the engine rows of a Zoho record (DESIGN §4.5).

    manage.py quotes_erase --reference Q-482913
    manage.py quotes_erase --zoho-record <id>      a v3 GAQL record, or a legacy GAQL / form source

Zoho itself is erased by hand.
"""

from django.core.management.base import BaseCommand, CommandError

from quotes.models import QuoteSubmission
from quotes.tasks.maintenance import erase_quote_pii, erase_zoho_record


class Command(BaseCommand):
    help = 'Erase the PII of a v3 quote and its crm rows (DESIGN §4.5).'

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--reference', metavar='Q-…')
        target.add_argument('--zoho-record', dest='zoho_record', metavar='ID')

    def handle(self, *args, reference: str | None, zoho_record: str | None, **options):
        if reference:
            quote = QuoteSubmission.objects.filter(reference=reference.strip().upper()).first()
            if quote is None:
                raise CommandError(f'no quote {reference}')
            erase_quote_pii(quote)
            self.stdout.write(self.style.SUCCESS(f'erased {quote.reference}'))
            return
        rows = erase_zoho_record(zoho_record.strip())
        if not rows:
            raise CommandError(f'nothing stored for Zoho record {zoho_record}')
        self.stdout.write(self.style.SUCCESS(f'erased {rows} row(s) of Zoho record {zoho_record}'))
