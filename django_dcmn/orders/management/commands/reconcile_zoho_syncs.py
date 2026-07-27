from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from orders.models import ZohoSyncJob
from orders.tasks import ORDER_TYPE_MAP, canonical_order_type, enqueue_zoho_sync


class Command(BaseCommand):
    help = 'Find eligible unsynced orders and persist/requeue their Zoho sync jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--since', default=None, help='ISO date, for example 2026-07-10')
        parser.add_argument('--order-type', default=None)
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        since = None
        if options['since']:
            try:
                since = datetime.fromisoformat(options['since'])
            except ValueError as exc:
                raise CommandError('--since must be an ISO date or datetime') from exc
            if timezone.is_naive(since):
                since = timezone.make_aware(since, dt_timezone.utc)

        selected_type = options['order_type']
        if selected_type:
            selected_type = canonical_order_type(selected_type)
            if selected_type not in ORDER_TYPE_MAP:
                raise CommandError(f'Unknown order type: {options["order_type"]}')

        found = []
        order_types = [selected_type] if selected_type else ORDER_TYPE_MAP.keys()
        for order_type in order_types:
            model_class, _ = ORDER_TYPE_MAP[order_type]
            queryset = model_class.objects.filter(zoho_synced=False).order_by('id')
            if since:
                queryset = queryset.filter(created_at__gte=since)
            if order_type in ('fbi', 'marriage'):
                queryset = queryset.filter(is_paid=True)

            suppressed_ids = ZohoSyncJob.objects.filter(
                order_type=order_type,
                status=ZohoSyncJob.STATUS_SUPPRESSED,
            ).values_list('order_id', flat=True)
            queryset = queryset.exclude(id__in=suppressed_ids)

            for order in queryset:
                found.append((order_type, order))

        if options['limit'] is not None:
            found = found[:options['limit']]

        for order_type, order in found:
            tracking_id = getattr(getattr(order, 'track', None), 'tid', None)
            self.stdout.write(f'{order_type} #{order.id}')
            if not options['dry_run']:
                enqueue_zoho_sync(order.id, order_type, tracking_id=tracking_id)

        action = 'would queue' if options['dry_run'] else 'queued'
        self.stdout.write(self.style.SUCCESS(f'{action} {len(found)} Zoho sync job(s)'))
