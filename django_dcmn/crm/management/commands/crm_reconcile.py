"""Re-arm failed service links (DESIGN §12.3): `manage.py crm_reconcile [--since YYYY-MM-DD] [--dry-run]`."""

from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm import flags
from crm.models import EngineRequest, ServiceLink
from crm.tasks import run_service_link


class Command(BaseCommand):
    help = 'Re-arm failed ServiceLinks (state → pending) and queue them. Never touches legacy ZohoSyncJob rows.'

    def add_arguments(self, parser):
        parser.add_argument('--since', help='only links created on or after this date (YYYY-MM-DD)')
        parser.add_argument('--dry-run', action='store_true', help='list what would be re-armed, change nothing')

    def handle(self, *args, since=None, dry_run=False, **options):
        links = ServiceLink.objects.filter(state='failed').order_by('created_at')
        if since:
            try:
                day = datetime.strptime(since, '%Y-%m-%d').date()
            except ValueError as exc:
                raise CommandError('--since must be YYYY-MM-DD') from exc
            links = links.filter(created_at__gte=timezone.make_aware(datetime.combine(day, time.min)))
        rows = list(links)
        for link in rows:
            self.stdout.write(f'{link.pk} {link.external_key} {link.last_error_code or "-"}')
        if dry_run:
            self.stdout.write(f'dry run: {len(rows)} failed link(s) would be re-armed')
            return
        if rows and flags.engine_mode() == 'off':
            raise CommandError('CRM_ENGINE_MODE is off: the links would not run. Nothing was changed.')

        ids = [link.pk for link in rows]
        # The audit row of this action; it also restarts each link's 24 h retry window.
        request = EngineRequest.objects.create(caller='admin', trigger='crm_reconcile', mode='create', state='running',
                                               services_requested=sorted({link.service_key for link in rows}))
        armed = ServiceLink.objects.filter(pk__in=ids, state='failed').update(
            state='pending', attempts=0, next_attempt_at=None, lease_until=None, request=request,
            updated_at=timezone.now())
        for link_id in ids:
            transaction.on_commit(lambda link_id=link_id: run_service_link.delay(link_id))
        self.stdout.write(self.style.SUCCESS(f're-armed {armed} link(s)'))
