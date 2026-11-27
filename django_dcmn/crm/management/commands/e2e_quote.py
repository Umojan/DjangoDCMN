"""End-to-end run with ZZ TEST data (DESIGN §14.3), every run approved by Vlad.

    manage.py e2e_quote --run ID (--dry-run|--execute) --confirm-org <ZOHO_ORG_ID> [--scenario E1,E4,…]

A person performs each scenario's step (the staging page, the Zoho buttons, the panel). This command prints the
test data and the steps; with --execute it runs the server-side steps (E3), collects every Zoho id the run
touched into `var/e2e-<id>.json` (the manifest `e2e_cleanup` works from) and runs the read-only checks.

Guards: the command refuses to run unless `--confirm-org` names the configured ZOHO_ORG_ID (the run is meant for
that org, i.e. production); `--execute` also needs `E2E_ALLOWED=1`. `--dry-run` reads and writes nothing.
"""

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from crm import flags, zoho
from crm.adoption import workflow_copies
from crm.management.local_files import private_dir
from crm.models import EngineRequest, ServiceLink
from crm.registry import COPY_RULE_IDS
from quotes import contract
from quotes.models import GAQL_MODULE, QuoteSubmission
from quotes.pipeline import ATTRIBUTION_MODULE, attribution_key

RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,19}$')
MANIFEST_DIR = Path(settings.BASE_DIR) / 'var'
DIRTY_TARGET_EVENTS = ('webhook_invoked', 'email_notification_sent', 'create_record')


@dataclass(frozen=True)
class Scenario:
    title: str
    step: str          # what the person does ({name}, {email}, {phone} are this run's test data)
    checks: str        # what the command checks afterwards (or what to check by hand)


SCENARIOS: dict[str, Scenario] = {
    'E1': Scenario('Quote from the staging page',
                   'Set E2E_ATTRIBUTION=1 for this run. On the staging page submit apostille + translation add-on + '
                   'fbi as {name} / {email} / {phone}.',
                   'synced; contact linked; attribution row; GAQL timeline has no create_record; no service record '
                   '(QUOTE_AUTO_INTAKE=none), every line REQUESTED; staff email in the daily thread (by hand)'),
    'E2': Scenario('Retry with the same submission',
                   'Re-send the E1 request (same submission_id and spent Turnstile token), then once more with an '
                   'edited message.',
                   'by hand: 200 duplicate, same reference, no 403, no siteverify call in the Railway log, amendment '
                   'Note'),
    'E3': Scenario('Honeypot and time trap', 'none: the command submits both bodies itself (--execute).',
                   '200 with reference null and nothing saved, for each'),
    'E4': Scenario('Create Translation (warm token)', 'Click TEST · Create Translation on the E1 GAQL record.',
                   'key search finds exactly the linked record; Tracking_ID set; target timeline has no '
                   'webhook_invoked / email_notification_sent / create_record; Resend logs by hand'),
    'E4b': Scenario('Create Translation (cold token)',
                    'Vlad clears zoho_access_token from the cache; submit a new quote as {name}; click TEST · Create '
                    'Translation.', '202 at once and the record within 30 s (Railway request log by hand); as E4'),
    'E5': Scenario('Click again, double click', 'Click the E4 button again, then double-click it.',
                   'one link and one record per service (key search)'),
    'E6': Scenario('Create all services',
                   'Submit embassy + mobile-notary + i9 + fbi as {name}; click Create all services.',
                   'as E4 for every created record; apostille suggestion line'),
    'E7': Scenario('Workflow copy adopted',
                   'Re-classify a ZZ TEST legacy-style GAQL record to Apostille Service, then click Create Apostille.',
                   'an adopted_copy link whose record a copy rule added'),
    'E8': Scenario('Manual record linked',
                   'Make a ZZ TEST Apostille by hand for the contact {email}, submit a new quote as {name}, then '
                   'Link this record in the panel.', 'an adopted_manual link'),
    'E9': Scenario('Zoho sync paused', 'QUOTE_V3_ZOHO_SYNC=False for one submission as {name}, then on.',
                   'by hand: paused → pending → synced; the staff email went out without Zoho links'),
    'E10': Scenario('Translation checkbox (Apostille, Embassy, Fingerprinting)',
                    'Tick the translation checkbox on the three staging forms as {name}.',
                    'by hand: fields on the record; suggestion + Note; Add translation makes one record'),
    'E10b': Scenario('Translation checkbox (FBI, Marriage)',
                     'On the test Railway service with Stripe test keys, pay a ZZ TEST order with the checkbox.',
                     'by hand: as E10 for Deals and Triple_Seal_Apostilles'),
    'E11': Scenario('Dry-run click', 'With CRM_ENGINE_MODE=dry_run, click a Create button on a run record.',
                    'a dry_run request that changed no link'),
    'E13': Scenario('Subform PUT semantics (S8 only)', 'QUOTE_V3_SUBFORM on for one run quote.',
                    'by hand: row ids kept, one row added, no duplicates'),
    'E14': Scenario('Auto-intake (only if A1 = yes)',
                    'QUOTE_AUTO_INTAKE=primary, engine live: one clean quote as {name}, one with two suspicion '
                    'signals.', 'by hand: the clean one gets its primary record; the suspect one gets the Note only'),
}
DEFAULT_SCENARIOS = [key for key in SCENARIOS if key != 'E13']
ALIASES = {'subform': 'E13'}


# --- Shared with e2e_cleanup -------------------------------------------------------------------------

def add_run_arguments(parser) -> None:
    parser.add_argument('--run', required=True, help='run id (letters, digits, dashes; ≤20)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='print what would happen; read and write nothing')
    mode.add_argument('--execute', action='store_true', help='needs E2E_ALLOWED=1')
    parser.add_argument('--confirm-org', default='', help='must equal ZOHO_ORG_ID: this run is meant for that org')
    parser.add_argument('--manifest-dir', default=str(MANIFEST_DIR))


def check_guards(run: str, *, execute: bool, confirm_org: str) -> None:
    """Refuse unless the run id is sane, --confirm-org names this org, and (for --execute) E2E_ALLOWED=1."""
    if not RUN_ID.match(run or ''):
        raise CommandError('Refused: --run must be letters, digits and dashes (at most 20).')
    if confirm_org != flags.org_id():
        raise CommandError('Refused: pass --confirm-org with the ZOHO_ORG_ID this run is meant for. '
                           'Nothing was read or written.')
    if execute and not flags.e2e_allowed():
        raise CommandError('Refused: --execute needs E2E_ALLOWED=1 for this run. Nothing was read or written.')


def manifest_path(manifest_dir: str, run: str) -> Path:
    return Path(manifest_dir) / f'e2e-{run}.json'


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def write_manifest(path: Path, manifest: dict) -> None:
    private_dir(path.parent)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(path)


def run_name(run: str) -> str:
    return f'ZZ TEST {run} Maria'


def run_quotes(run: str):
    return QuoteSubmission.objects.filter(full_name__istartswith=f'ZZ TEST {run} ').order_by('pk')


def run_email(run: str, scenario: str) -> str:
    """A plus-address of E2E_EMAIL_BASE (Vlad's mailbox), one per scenario."""
    base = flags.e2e_email_base()
    if '@' not in base:
        return '<set E2E_EMAIL_BASE>'
    local, _, domain = base.partition('@')
    tag = f'{run}-{scenario}'.lower()
    return f'{local}-{tag}@{domain}' if '+' in local else f'{local}+{tag}@{domain}'


def run_phone(index: int) -> str:
    return f'+1 202 555 01{index % 100:02d}'


# --- The command -------------------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'End-to-end run with ZZ TEST data: steps, the server-side steps, the manifest and the read-only checks.'

    def add_arguments(self, parser):
        add_run_arguments(parser)
        parser.add_argument('--scenario', default='', help='comma list, e.g. E1,E4,E4b (default: all but E13)')

    def handle(self, *args, run, dry_run=False, execute=False, confirm_org='', manifest_dir=str(MANIFEST_DIR),
               scenario='', **options):
        check_guards(run, execute=execute, confirm_org=confirm_org)
        selected = self._scenarios(scenario)
        for index, key in enumerate(selected, start=1):
            item = SCENARIOS[key]
            data = {'name': run_name(run), 'email': run_email(run, key), 'phone': run_phone(index)}
            self.stdout.write(f'{key} · {item.title}\n  do:    {item.step.format(**data)}\n  check: {item.checks}')
        if dry_run:
            self.stdout.write(f'dry run: {len(selected)} scenario(s); nothing was read or written')
            return

        path = manifest_path(manifest_dir, run)
        manifest = read_manifest(path) or {'run': run, 'org': flags.org_id(),
                                           'started_at': timezone.now().isoformat()}
        checks: list[dict] = []
        if 'E3' in selected:
            checks += self._spam_traps(run)
        manifest.update(collect(run))
        checks += run_checks(manifest, selected)
        manifest['checks'] = {**manifest.get('checks', {}), **{f'{c["scenario"]}:{c["check"]}': c for c in checks}}
        manifest['updated_at'] = timezone.now().isoformat()
        write_manifest(path, manifest)
        for check in checks:
            word = {True: 'PASS', False: 'FAIL', None: 'BY HAND'}[check['ok']]
            detail = f': {check["detail"]}' if check['detail'] else ''
            self.stdout.write(f'{word:8} {check["scenario"]} {check["check"]}{detail}')
        self.stdout.write(self.style.SUCCESS(f'manifest: {path}'))

    def _scenarios(self, raw: str) -> list[str]:
        if not raw:
            return list(DEFAULT_SCENARIOS)
        known = {**{key.lower(): key for key in SCENARIOS}, **ALIASES}
        parts = [part.strip() for part in raw.split(',') if part.strip()]
        keys = [known.get(part.lower(), part) for part in parts]
        unknown = [key for key in keys if key not in SCENARIOS]
        if unknown:
            raise CommandError(f'Unknown scenario(s): {", ".join(unknown)}')
        return list(dict.fromkeys(keys))

    def _spam_traps(self, run: str) -> list[dict]:
        """E3: the honeypot and time-trap bodies go through the real quote view; nothing may be saved."""
        from quotes.views import QuoteSubmissionView

        view, factory = QuoteSubmissionView.as_view(), RequestFactory()
        host = urlparse(str(getattr(settings, 'BASE_URL', '') or '')).hostname or 'localhost'
        results = []
        for fixture in ('honeypot', 'time_trap'):
            body = json.loads((contract.FIXTURES_DIR / f'{fixture}.json').read_text(encoding='utf-8'))['body']
            body['submission_id'] = str(uuid.uuid4())
            body['contact'] = {**body['contact'], 'full_name': run_name(run), 'email': run_email(run, 'E3'),
                               'phone': run_phone(3)}
            before = run_quotes(run).count()
            request = factory.post(reverse('quote_requests_submit'), data=json.dumps(body),
                                   content_type='application/json', HTTP_HOST=host)
            response = view(request)
            answer = getattr(response, 'data', None) or {}
            ok = response.status_code == 200 and answer.get('reference') is None and run_quotes(run).count() == before
            results.append({'scenario': 'E3', 'check': fixture, 'ok': ok,
                            'detail': f'HTTP {response.status_code}, reference {answer.get("reference")}'})
        return results


# --- Manifest --------------------------------------------------------------------------------------

def collect(run: str) -> dict:
    """Every Zoho id this run touched, from Django: quotes, contacts, attribution rows, records, copies, TIDs."""
    quotes, records, copies, tids = [], [], [], []
    for quote in run_quotes(run):
        quotes.append({'pk': quote.pk, 'reference': quote.reference, 'gaql_id': quote.zoho_record_id,
                       'contact_id': quote.zoho_contact_id, 'contact_action': quote.zoho_contact_action,
                       'attribution_id': quote.zoho_attribution_id, 'attribution_key': attribution_key(quote)})
        for link in ServiceLink.objects.filter(source_key=f'qr:{quote.pk}').exclude(target_record_id=''):
            item = {'module': link.target_module, 'id': link.target_record_id, 'service': link.service_key,
                    'link_id': link.pk, 'origin': link.origin, 'tracking_id': link.tracking_id,
                    'gaql_id': quote.zoho_record_id}
            (copies if link.origin == 'adopted_copy' else records).append(item)
            if link.tracking_id:
                tids.append(link.tracking_id)
    return {'quotes': quotes, 'records': records, 'copies': copies, 'tids': sorted(set(tids))}


# --- Checks (read-only) ----------------------------------------------------------------------------

def _check(scenario: str, check: str, ok: bool | None, detail: str = '') -> dict:
    return {'scenario': scenario, 'check': check, 'ok': ok, 'detail': detail}


def run_checks(manifest: dict, selected: list[str]) -> list[dict]:
    checks: list[dict] = []
    quotes = manifest.get('quotes') or []
    if 'E1' in selected:
        checks += _e1_checks(quotes)
    if any(key in selected for key in ('E4', 'E4b', 'E5', 'E6')):
        checks += _record_checks(manifest.get('records') or [])
    if 'E7' in selected:
        checks += _copy_checks(manifest.get('copies') or [])
    if 'E8' in selected:
        found = [r for r in manifest.get('records') or [] if r.get('origin') == 'adopted_manual']
        checks.append(_check('E8', 'adopted_manual link', bool(found), f'{len(found)} link(s)'))
    if 'E11' in selected:
        gaql_ids = [q['gaql_id'] for q in quotes if q.get('gaql_id')]
        dry = EngineRequest.objects.filter(source_module=GAQL_MODULE, source_record_id__in=gaql_ids, mode='dry_run')
        untouched = dry.exists() and not ServiceLink.objects.filter(request__in=dry).exists()
        checks.append(_check('E11', 'dry_run click changed no link', untouched, f'{dry.count()} dry-run request(s)'))
    for key in selected:
        if key in ('E2', 'E4b', 'E6', 'E9', 'E10', 'E10b', 'E13', 'E14'):
            checks.append(_check(key, 'by hand', None, SCENARIOS[key].checks))
    return checks


def _e1_checks(quotes: list[dict]) -> list[dict]:
    if not quotes:
        return [_check('E1', 'quote saved', False, 'no ZZ TEST quote of this run')]
    first = QuoteSubmission.objects.filter(pk=quotes[0]['pk']).first()
    checks = [_check('E1', 'quote saved', first is not None, quotes[0]['reference'])]
    if first is None:
        return checks
    checks += [
        _check('E1', 'synced', first.sync_status == 'synced', first.sync_status),
        _check('E1', 'contact linked', bool(first.zoho_contact_id), first.zoho_contact_action),
        _check('E1', 'attribution row', bool(first.zoho_attribution_id) if flags.e2e_attribution() else None,
               ATTRIBUTION_MODULE),
        _check('E1', 'no service record', not ServiceLink.objects.filter(
            source_key=f'qr:{first.pk}', trigger='intake', state__in=('created', 'adopted')).exists()),
    ]
    if first.zoho_record_id:
        try:
            events = zoho.timeline(GAQL_MODULE, first.zoho_record_id)
            made = [e for e in events if e.get('action') == 'create_record']
            checks.append(_check('E1', 'GAQL timeline has no create_record', not made, f'{len(made)} event(s)'))
        except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
            checks.append(_check('E1', 'GAQL timeline has no create_record', False, getattr(exc, 'code', 'error')))
    return checks


def _record_checks(records: list[dict]) -> list[dict]:
    checks = []
    created = [r for r in records if r.get('origin') in ('created', '')]
    if not created:
        return [_check('E4', 'created record', False, 'no record created by this run yet')]
    for record in created:
        label = f'{record["service"]} {record["module"]}/{record["id"]}'
        link = ServiceLink.objects.filter(pk=record['link_id']).first()
        try:
            found = zoho.search_by_key(record['module'], link.external_key) if link else None
            checks.append(_check('E5', f'{label}: key finds this record only',
                                 bool(found) and str(found.get('id')) == str(record['id'])))
            events = zoho.timeline(record['module'], record['id'])
            dirty = sorted({e.get('action') for e in events if e.get('action') in DIRTY_TARGET_EVENTS})
            checks.append(_check('E4', f'{label}: timeline clean', not dirty, ', '.join(dirty)))
        except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
            checks.append(_check('E4', f'{label}: Zoho read', False, getattr(exc, 'code', 'error')))
        checks.append(_check('E4', f'{label}: Tracking_ID set', bool(record.get('tracking_id')) or None,
                             'no TID policy for this module' if not record.get('tracking_id') else ''))
    return checks


def _copy_checks(copies: list[dict]) -> list[dict]:
    if not copies:
        return [_check('E7', 'adopted_copy link', False, 'none yet')]
    checks = []
    for copy in copies:
        try:
            ok = is_rule_copy(copy['module'], copy['id'], copy.get('gaql_id') or '')
        except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
            ok = False
            copy = {**copy, 'error': getattr(exc, 'code', 'error')}
        checks.append(_check('E7', f'{copy["module"]}/{copy["id"]}: made by a copy rule', ok, copy.get('error', '')))
    return checks


def is_rule_copy(module: str, record_id: str, gaql_id: str) -> bool:
    """The GAQL timeline has the workflow create_record of this record AND its own timeline was `added` by a
    COPY_RULE_IDS rule (DESIGN E12)."""
    if not gaql_id:
        return False
    if record_id not in {copy_id for _, copy_id in workflow_copies(zoho.timeline(GAQL_MODULE, gaql_id), module)}:
        return False
    for event in zoho.timeline(module, record_id):
        rule = (event.get('automation_details') or {}).get('rule') or {}
        if event.get('action') == 'added' and event.get('source') == 'workflow' \
                and str(rule.get('id')) in COPY_RULE_IDS:
            return True
    return False
