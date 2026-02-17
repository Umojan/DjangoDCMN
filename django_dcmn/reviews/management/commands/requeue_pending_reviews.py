from django.core.management.base import BaseCommand
from django.utils import timezone

from reviews.models import ReviewRequest
from reviews.tasks import (
    process_review_request_task,
    _send_google_review_email,
    _send_trustpilot_email,
)


class Command(BaseCommand):
    help = 'Re-queue stuck PENDING review requests. Dry run by default.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            default=False,
            help='Actually send emails / requeue tasks. Without this flag, only shows what would happen.',
        )

    def handle(self, *args, **options):
        execute = options['execute']
        pending = ReviewRequest.objects.filter(is_sent=False)

        # Split into two groups
        new_records = pending.filter(review_type='')
        typed_records = pending.exclude(review_type='')

        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"Stuck PENDING review requests: {pending.count()}")
        self.stdout.write(f"  NEW (no type, 0→0): {new_records.count()} — will requeue full task")
        self.stdout.write(f"  TYPED (has type):   {typed_records.count()} — will send email only")
        self.stdout.write(f"{'=' * 60}\n")

        if not execute:
            self.stdout.write(self.style.WARNING('DRY RUN — no emails sent. Use --execute to send.\n'))

        # --- Group 1: NEW records — full requeue ---
        if new_records.exists():
            self.stdout.write(self.style.HTTP_INFO('--- NEW records (full requeue) ---'))
            for rr in new_records:
                self.stdout.write(f"  #{rr.id} {rr.email} | {rr.zoho_module}")
                if execute:
                    process_review_request_task.delay(rr.id)
                    self.stdout.write(self.style.SUCCESS(f"    → Queued task"))

        # --- Group 2: TYPED records — email only ---
        if typed_records.exists():
            self.stdout.write(self.style.HTTP_INFO('\n--- TYPED records (email only) ---'))
            sent = 0
            failed = 0
            for rr in typed_records:
                self.stdout.write(
                    f"  #{rr.id} {rr.email} | {rr.review_type.upper()} | "
                    f"leads_won {rr.leads_won_before}→{rr.leads_won_after} | {rr.zoho_module}"
                )
                if execute:
                    try:
                        if rr.review_type == 'google':
                            _send_google_review_email(rr)
                        elif rr.review_type == 'trustpilot':
                            _send_trustpilot_email(rr)
                        else:
                            self.stdout.write(self.style.WARNING(f"    → Unknown type '{rr.review_type}', skipping"))
                            continue

                        rr.is_sent = True
                        rr.sent_at = timezone.now()
                        rr.save(update_fields=['is_sent', 'sent_at'])
                        sent += 1
                        self.stdout.write(self.style.SUCCESS(f"    → Sent {rr.review_type} email"))
                    except Exception as e:
                        failed += 1
                        self.stdout.write(self.style.ERROR(f"    → FAILED: {e}"))

            if execute:
                self.stdout.write(f"\nTyped results: {sent} sent, {failed} failed")

        self.stdout.write(self.style.SUCCESS(f'\nDone.'))
