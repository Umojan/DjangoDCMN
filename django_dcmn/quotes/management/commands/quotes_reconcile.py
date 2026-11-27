"""Re-arm `failed` v3 quotes from their failed step (DESIGN §12.3).

    manage.py quotes_reconcile --since YYYY-MM-DD [--dry-run]

Legacy `ZohoSyncJob` rows are never touched.
"""

from datetime import datetime, time, timezone

from django.core.management.base import BaseCommand, CommandError

from quotes import pipeline
from quotes.models import QuoteSubmission


class Command(BaseCommand):
    help = 'Re-arm failed v3 quote submissions created since a date (DESIGN §12.3).'

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, metavar='YYYY-MM-DD', help='created on or after this UTC day')
        parser.add_argument('--dry-run', action='store_true', help='list what would be re-armed, change nothing')

    def handle(self, *args, since: str, dry_run: bool, **options):
        try:
            day = datetime.strptime(since, '%Y-%m-%d').date()
        except ValueError as exc:
            raise CommandError('--since must be YYYY-MM-DD') from exc
        start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        rows = QuoteSubmission.objects.filter(sync_status='failed', created_at__gte=start).order_by('created_at')
        rearmed = 0
        for quote in rows:
            if dry_run:
                self.stdout.write(f'would re-arm {quote.reference} ({quote.sync_last_error or "no error"})')
                continue
            if pipeline.rearm(quote):
                rearmed += 1
                self.stdout.write(f're-armed {quote.reference}')
        total = rows.count() if dry_run else rearmed
        self.stdout.write(self.style.SUCCESS(f'{"would re-arm" if dry_run else "re-armed"} {total}'))
