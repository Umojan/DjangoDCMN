"""Delete what an e2e run made (DESIGN §14.3 E12), dry run first, with Vlad's OK.

    manage.py e2e_cleanup --run ID (--dry-run|--execute) --confirm-org <ZOHO_ORG_ID>

Works from the run's manifest `var/e2e-<id>.json` only, and each module by its own rule, re-checked live:
- service records and GAQL: id in the manifest AND the name (GAQL: `Client_Name`) starts with "ZZ TEST";
- workflow copies (bare names such as "Apostille Service"): the manifest GAQL's timeline has the workflow
  `create_record` of that record AND the record's own timeline was `added` by a COPY_RULE_IDS rule;
- attribution rows: the row holding the key `dcmn:attribution:qr:<pk>` of a manifest quote;
- contacts: only those this run created, with a "ZZ TEST" name;
- `Track` rows: manifest TIDs (and each copy's `Tracking_ID`) whose row names no record outside the manifest;
- then the run's Django rows (ZZ TEST quotes, their links and signals). EngineRequests stay (the audit).

Deletes use `wf_trigger=false`; deleted records sit in the Zoho recycle bin. Guards as in `e2e_quote`.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm import zoho
from crm.facts import SOURCE_FIELD_MAPS, is_test_name
from crm.models import ServiceLink, ServiceSignal
from crm.registry import REGISTRY
from orders.models import Track
from quotes.models import GAQL_MODULE, QuoteSubmission
from quotes.pipeline import ATTRIBUTION_MODULE

from .e2e_quote import MANIFEST_DIR, add_run_arguments, check_guards, is_rule_copy, manifest_path, read_manifest, \
    write_manifest

CONTACTS_MODULE = 'Contacts'
NAME_FIELDS = {**{spec.module: spec.name_field for spec in REGISTRY.values()},
               GAQL_MODULE: SOURCE_FIELD_MAPS[GAQL_MODULE]['client_name'], CONTACTS_MODULE: 'Full_Name'}


class Command(BaseCommand):
    help = 'Delete the Zoho records, Track rows and Django rows of one e2e run, each by its own manifest rule.'

    def add_arguments(self, parser):
        add_run_arguments(parser)

    def handle(self, *args, run, dry_run=False, execute=False, confirm_org='', manifest_dir=str(MANIFEST_DIR),
               **options):
        check_guards(run, execute=execute, confirm_org=confirm_org)
        path = manifest_path(manifest_dir, run)
        manifest = read_manifest(path)
        if not manifest:
            raise CommandError(f'No manifest at {path}. Nothing was read or written.')
        plan = build_plan(manifest)
        for item in plan:
            verdict = 'DELETE' if item['delete'] else 'KEEP'
            self.stdout.write(f'{verdict:6} {item["kind"]:11} {item["module"]}/{item["id"]} · {item["reason"]}')
        doomed = [item for item in plan if item['delete']]
        if dry_run:
            self.stdout.write(f'dry run: {len(doomed)} of {len(plan)} item(s) would be deleted; nothing was written')
            return

        deleted, failed = [], []
        for item in doomed:
            try:
                delete_item(item)
                deleted.append(item)
            except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
                failed.append({**item, 'error': getattr(exc, 'code', '') or type(exc).__name__})
                self.stderr.write(f'FAILED {item["kind"]} {item["module"]}/{item["id"]}: {failed[-1]["error"]}')
        rows = delete_django_rows(manifest) if not failed else 0
        manifest['cleanup'] = {'at': timezone.now().isoformat(), 'deleted': deleted, 'failed': failed,
                               'django_quotes_deleted': rows}
        write_manifest(path, manifest)
        message = f'deleted {len(deleted)} item(s), {len(failed)} failed, {rows} Django quote(s) removed'
        self.stdout.write(self.style.SUCCESS(message) if not failed else self.style.WARNING(
            f'{message}; Django rows kept until every Zoho delete succeeds'))


def _item(kind: str, module: str, record_id: str, delete: bool, reason: str) -> dict:
    return {'kind': kind, 'module': module, 'id': str(record_id), 'delete': delete, 'reason': reason}


def _named_test(kind: str, module: str, record_id: str) -> dict:
    """Delete only when the record still exists and its name starts with "ZZ TEST"."""
    field = NAME_FIELDS.get(module, 'Name')
    record = zoho.get(module, record_id, fields=[field])
    if record is None:
        return _item(kind, module, record_id, False, 'already gone')
    name = record.get(field)
    if not is_test_name(name):
        return _item(kind, module, record_id, False, f'{field} does not start with ZZ TEST')
    return _item(kind, module, record_id, True, f'{field} starts with ZZ TEST')


def build_plan(manifest: dict) -> list[dict]:
    """Every manifest item with its verdict; Zoho is only read here."""
    plan: list[dict] = []
    quotes = manifest.get('quotes') or []
    for record in manifest.get('records') or []:
        plan.append(_named_test('record', record['module'], record['id']))
    copy_tids = []
    for copy in manifest.get('copies') or []:
        if is_rule_copy(copy['module'], copy['id'], copy.get('gaql_id') or ''):
            plan.append(_item('copy', copy['module'], copy['id'], True, 'GAQL create_record + copy-rule added'))
            found = zoho.get(copy['module'], copy['id'], fields=['Tracking_ID']) or {}
            if found.get('Tracking_ID'):
                copy_tids.append(str(found['Tracking_ID']))
        else:
            plan.append(_item('copy', copy['module'], copy['id'], False, 'not a workflow copy of a manifest GAQL'))
    for quote in quotes:
        plan.append(_attribution(quote))
    for quote in quotes:
        if quote.get('gaql_id'):
            plan.append(_named_test('gaql', GAQL_MODULE, quote['gaql_id']))
    for quote in quotes:
        if not quote.get('contact_id'):
            continue
        if quote.get('contact_action') != 'created':
            plan.append(_item('contact', CONTACTS_MODULE, quote['contact_id'], False, 'not created by this run'))
        else:
            plan.append(_named_test('contact', CONTACTS_MODULE, quote['contact_id']))
    plan += _tracks(manifest, copy_tids)
    return plan


def _attribution(quote: dict) -> dict:
    """The row whose key is exactly `dcmn:attribution:qr:<pk>` of this manifest quote."""
    key = f'dcmn:attribution:qr:{quote["pk"]}'
    record_id = quote.get('attribution_id') or ''
    record = zoho.get(ATTRIBUTION_MODULE, record_id, fields=[zoho.KEY_FIELD]) if record_id else \
        zoho.search_by_key(ATTRIBUTION_MODULE, key)
    if record is None:
        return _item('attribution', ATTRIBUTION_MODULE, record_id or '-', False, 'already gone')
    if record.get(zoho.KEY_FIELD) != key:
        return _item('attribution', ATTRIBUTION_MODULE, record.get('id') or record_id, False,
                     f'key is not {key}')
    return _item('attribution', ATTRIBUTION_MODULE, record.get('id') or record_id, True, f'key {key}')


def _tracks(manifest: dict, copy_tids: list[str]) -> list[dict]:
    """Manifest TIDs whose Track row names no record, or only a manifest record."""
    ours = {str(item['id']) for key in ('records', 'copies') for item in manifest.get(key) or []}
    items = []
    for tid in dict.fromkeys([*(manifest.get('tids') or []), *copy_tids]):
        track = Track.objects.filter(tid=tid).first()
        if track is None:
            items.append(_item('track', 'Track', tid, False, 'already gone'))
            continue
        record_id = str((track.data or {}).get('record_id') or '')
        if record_id and record_id not in ours:
            items.append(_item('track', 'Track', tid, False, 'names a record outside the manifest'))
        else:
            items.append(_item('track', 'Track', tid, True, 'manifest TID'))
    return items


def delete_item(item: dict) -> None:
    if item['kind'] == 'track':
        Track.objects.filter(tid=item['id']).delete()
        return
    path = f'{item["module"]}/{item["id"]}'
    zoho.result_id(zoho.zoho_call('DELETE', path, params={'wf_trigger': 'false'}), 'DELETE', path)


def delete_django_rows(manifest: dict) -> int:
    """The run's ZZ TEST quotes (lines, phone matches and signals cascade) and their links."""
    pks = [quote['pk'] for quote in manifest.get('quotes') or []]
    quotes = QuoteSubmission.objects.filter(pk__in=pks, is_test=True)
    keys = [f'qr:{pk}' for pk in quotes.values_list('pk', flat=True)]
    with transaction.atomic():
        ServiceLink.objects.filter(source_key__in=keys).delete()
        ServiceSignal.objects.filter(source_key__in=keys).delete()
        quotes.delete()
    return len(keys)
