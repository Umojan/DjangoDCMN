"""Plan (default), apply or verify the additive Zoho fields of `crm/schema_spec.py` (DESIGN §6.6).

    manage.py zoho_schema [--module M]... [--json PATH]          plan: GET requests only, writes nothing
    manage.py zoho_schema --apply --confirm-org org882204370 [--only A,B] [--module M]
    manage.py zoho_schema --verify                               plan, and MANUAL items fail until seen

`--apply` runs only with ZOHO_SCHEMA_APPLY_ALLOWED=1, `--confirm-org` equal to ZOHO_ORG_ID, the prod GAQL
layout present (org fingerprint) and a plan without MISMATCH or BLOCKED. It creates the ADD items only, one
POST per field, 1 s apart, re-reads each and stops at the first surprise. It never sends a PUT, so existing
fields and picklists (GET_A_QUOTE_LEADS included) are never edited. Each apply run is logged (no token) to
ZOHO_SCHEMA_LOG_DIR/zoho_schema_<UTC>.json.

Exit codes: 0 OK; 1 MISMATCH or BLOCKED (and --verify not clean); 2 refused; 3 aborted mid-apply.
"""

import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm import flags, schema, schema_spec, zoho
from crm.management.local_files import private_dir
from crm.schema import ADD, BLOCKED, MANUAL, MISMATCH, OK, PlanItem
from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

EXIT_PLAN_PROBLEM = 1
EXIT_REFUSED = 2
EXIT_ABORTED = 3
APPLY_PAUSE_S = 1.0
RESULTS = {EXIT_PLAN_PROBLEM: 'plan_problem', EXIT_REFUSED: 'refused', EXIT_ABORTED: 'aborted'}
ZOHO_ERRORS = (ZohoTransientError, ZohoPermanentError)


def _fail(code: int, message: str) -> CommandError:
    return CommandError(message, returncode=code)


def _error_info(exc: Exception) -> dict:
    """What the run log keeps of a crm.zoho error: class, Zoho code, HTTP status, field and details."""
    return {
        'type': type(exc).__name__,
        'code': getattr(exc, 'code', ''),
        'http_status': getattr(exc, 'http_status', None),
        'field': getattr(exc, 'field', ''),
        'details': getattr(exc, 'details', {}),
    }


class Command(BaseCommand):
    help = 'Plan (default), apply or verify the additive Zoho fields of crm/schema_spec.py (DESIGN §6.6).'

    def add_arguments(self, parser):
        parser.add_argument('--module', action='append', dest='modules', metavar='MODULE',
                            help='limit to this module (repeatable); default: every spec module')
        parser.add_argument('--json', dest='json_path', metavar='PATH',
                            help='also write the plan and the raw field metadata to PATH')
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='create the ADD items (every guard must pass)')
        mode.add_argument('--verify', action='store_true', help='exit 1 unless every API and MANUAL item is OK')
        parser.add_argument('--confirm-org', dest='confirm_org', metavar='ORG', help='must equal ZOHO_ORG_ID')
        parser.add_argument('--only', metavar='A,B', help='with --apply: create only these api_names')

    def handle(self, *args, **options):
        modules = self._modules(options['modules'])
        if options['apply']:
            self._apply(modules, options)
            return
        if options['only'] or options['confirm_org']:
            self.stdout.write('Plan only: --only and --confirm-org take effect with --apply. Nothing is written.')
        metadata = self._read(modules)
        items = schema.plan_against(metadata, modules)
        self._print(items)
        mode = 'verify' if options['verify'] else 'plan'
        self._write_json(options['json_path'], mode, metadata, items)
        if options['verify']:
            self._verify(modules, metadata, items)
            return
        self._raise_on_plan_problems(items)

    # --- plan and verify ---------------------------------------------------------------------------

    def _modules(self, requested: list[str] | None) -> list[str]:
        if not requested:
            return list(schema_spec.SPEC)
        unknown = [m for m in requested if m not in schema_spec.SPEC]
        if unknown:
            raise _fail(EXIT_REFUSED, f'Not a spec module: {", ".join(unknown)}. Known: {", ".join(schema_spec.SPEC)}')
        return list(dict.fromkeys(requested))

    def _read(self, modules: list[str]) -> dict[str, list[dict]]:
        try:
            return schema.fetch_metadata(modules)
        except ZOHO_ERRORS as exc:
            code = getattr(exc, 'code', '') or exc
            raise _fail(EXIT_REFUSED, f'Could not read Zoho field metadata ({code})') from exc

    def _print(self, items: list[PlanItem]) -> None:
        module = None
        for item in items:
            if item.module != module:
                module = item.module
                self.stdout.write(module)
            style = self.style.ERROR if item.action in (MISMATCH, BLOCKED) else (lambda text: text)
            self.stdout.write(style(f'  {item.line()}'))
        counts = Counter(item.action for item in items)
        self.stdout.write(' · '.join(f'{action} {counts.get(action, 0)}'
                                     for action in (ADD, OK, MANUAL, MISMATCH, BLOCKED)))

    def _raise_on_plan_problems(self, items: list[PlanItem]) -> None:
        problems = [item for item in items if item.action in (MISMATCH, BLOCKED)]
        if problems:
            raise _fail(EXIT_PLAN_PROBLEM, f'{len(problems)} MISMATCH/BLOCKED item(s). They are never fixed '
                                           'automatically: fix Zoho by hand or crm/schema_spec.py by PR.')

    def _verify(self, modules: list[str], metadata: dict[str, list[dict]], items: list[PlanItem]) -> None:
        failures = schema.verify_failures(items)
        live = {module: schema.by_api_name(schema.normalise(metadata[module])) for module in modules}
        problems = schema.validate_registry(live)
        for problem in problems:
            self.stdout.write(self.style.ERROR(f'PROBLEM registry: {problem}'))
        if failures or problems:
            raise _fail(EXIT_PLAN_PROBLEM,
                        f'verify: {len(failures)} item(s) not OK, {len(problems)} registry problem(s)')
        self.stdout.write(self.style.SUCCESS('verify: every item is OK'))

    def _write_json(self, path: str | None, mode: str, metadata: dict[str, list[dict]], items: list[PlanItem]) -> None:
        if not path:
            return
        plan = []
        for item in items:
            row = asdict(item)
            if item.action == ADD and item.kind == 'field':
                row['payload'] = schema_spec.find(item.module, item.api_name).create_payload()
            plan.append(row)
        target = Path(path)
        private_dir(target.parent)
        document = {'generated_at': timezone.now().isoformat(), 'org': flags.org_id(), 'mode': mode,
                    'plan': plan, 'metadata': metadata}
        target.write_text(json.dumps(document, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        self.stdout.write(f'Wrote {target}')

    # --- apply -------------------------------------------------------------------------------------

    def _apply(self, modules: list[str], options: dict) -> None:
        log_dir = Path(flags.schema_log_dir())
        try:
            private_dir(log_dir)
        except OSError as exc:
            raise _fail(EXIT_REFUSED, f'ZOHO_SCHEMA_LOG_DIR {log_dir} is not writable') from exc
        started = timezone.now()
        run = {'command': 'zoho_schema --apply', 'started_at': started.isoformat(), 'org': flags.org_id(),
               'modules': modules, 'only': [], 'checks': [], 'plan': [], 'actions': []}
        exit_code = None   # stays None on an unexpected error
        try:
            only = self._only(options['only'], modules)
            run['only'] = sorted(only)
            self._check_settings(options['confirm_org'], run)
            self._check_org_fingerprint(run)
            metadata = self._read(modules)
            items = schema.plan_against(metadata, modules)
            run['plan'] = [asdict(item) for item in items]
            self._print(items)
            self._write_json(options['json_path'], 'apply', metadata, items)
            self._raise_on_plan_problems(items)
            todo = [item for item in items
                    if item.kind == 'field' and item.action == ADD and (not only or item.api_name in only)]
            if not todo:
                self.stdout.write('Nothing to create.')
            self._create(todo, run)
            exit_code = 0
            run['result'] = 'ok'
        except CommandError as exc:
            exit_code = exc.returncode
            run['result'] = RESULTS.get(exc.returncode, 'error')
            run['error'] = str(exc)
            raise
        finally:
            run.setdefault('result', 'error')
            run['exit_code'] = exit_code
            run['finished_at'] = timezone.now().isoformat()
            path = log_dir / f'zoho_schema_{started:%Y%m%dT%H%M%S.%fZ}.json'
            path.write_text(json.dumps(run, indent=1, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
            self.stdout.write(f'Log: {path}')

    def _only(self, raw: str | None, modules: list[str]) -> set[str]:
        only = {name.strip() for name in (raw or '').split(',') if name.strip()}
        creatable = {spec.api_name for module in modules for spec in schema_spec.SPEC[module]
                     if not spec.manual and not spec.existing}
        unknown = sorted(only - creatable)
        if unknown:
            raise _fail(EXIT_REFUSED, '--only names no API-created spec field of the selected modules: '
                                      + ', '.join(unknown))
        return only

    def _check_settings(self, confirm_org: str | None, run: dict) -> None:
        if not flags.schema_apply_allowed():
            raise _fail(EXIT_REFUSED, 'Refused: ZOHO_SCHEMA_APPLY_ALLOWED=1 is not set in the environment.')
        run['checks'].append({'check': 'apply_allowed', 'ok': True})
        if not confirm_org or confirm_org != flags.org_id():
            raise _fail(EXIT_REFUSED, f'Refused: --confirm-org must equal ZOHO_ORG_ID ({flags.org_id()}).')
        run['checks'].append({'check': 'confirm_org', 'ok': True})

    def _check_org_fingerprint(self, run: dict) -> None:
        """The prod GAQL layout must be visible to this token; GET /crm/v6/org must agree when the scope allows."""
        try:
            body = zoho.zoho_call('GET', 'settings/layouts', params={'module': schema_spec.GAQL})
        except ZOHO_ERRORS as exc:
            raise _fail(EXIT_REFUSED, f'Refused: could not read the {schema_spec.GAQL} layouts '
                                      f'({getattr(exc, "code", "")}).') from exc
        layout_ids = {str(layout.get('id')) for layout in (body or {}).get('layouts') or []}
        if schema_spec.GAQL_LAYOUT_ID not in layout_ids:
            raise _fail(EXIT_REFUSED, f'Refused: layout {schema_spec.GAQL_LAYOUT_ID} is not in {schema_spec.GAQL}; '
                                      'this token is not for the prod org.')
        run['checks'].append({'check': 'gaql_layout', 'ok': True, 'layout_id': schema_spec.GAQL_LAYOUT_ID})

        # The prod token has no ZohoCRM.org.READ [06-facts]: Zoho answers 401 OAUTH_SCOPE_MISMATCH (crm.zoho never
        # refreshes the token for it) and the layout fingerprint above is the guard.
        try:
            body = zoho.zoho_call('GET', 'org', api='v6')
        except ZohoPermanentError as exc:
            if getattr(exc, 'code', '') != 'OAUTH_SCOPE_MISMATCH':
                raise _fail(EXIT_REFUSED, f'Refused: GET /crm/v6/org failed ({getattr(exc, "code", "")}).') from exc
            run['checks'].append({'check': 'org', 'ok': None, 'detail': 'skipped: the token has no ZohoCRM.org.READ'})
            return
        except ZohoTransientError as exc:
            raise _fail(EXIT_REFUSED, f'Refused: GET /crm/v6/org failed ({getattr(exc, "code", "")}).') from exc
        orgs = (body or {}).get('org') or [{}]
        org = orgs[0] if isinstance(orgs[0], dict) else {}
        names = {str(org.get('domain_name') or '')} | ({f"org{org['zgid']}"} if org.get('zgid') else set())
        if flags.org_id() not in names:
            raise _fail(EXIT_REFUSED, f'Refused: GET /crm/v6/org is not {flags.org_id()}.')
        run['checks'].append({'check': 'org', 'ok': True})

    def _create(self, todo: list[PlanItem], run: dict) -> None:
        for index, item in enumerate(todo):
            spec = schema_spec.find(item.module, item.api_name)
            payload = spec.create_payload()
            action = {'module': item.module, 'api_name': item.api_name, 'payload': payload}
            run['actions'].append(action)
            if index:
                time.sleep(APPLY_PAUSE_S)
            where = f'{item.module}.{item.api_name}'
            try:
                response = zoho.zoho_call('POST', 'settings/fields', params={'module': item.module},
                                          json={'fields': [payload]}, api='v6')
            except ZOHO_ERRORS as exc:
                action['error'] = _error_info(exc)
                hint = (f' If Zoho refused the type, switch {spec.api_name} to its fallback '
                        f'{spec.fallback.api_name} in crm/schema_spec.py by PR (A2).') if spec.fallback else ''
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: Zoho refused the create '
                                          f'({getattr(exc, "code", "")}).{hint}') from exc
            action['response'] = response
            field_id = self._created_id(response)
            if not field_id:
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: no field id in the response.')
            action['field_id'] = field_id
            try:
                created = next((f for f in zoho.fields_metadata(item.module) if str(f.get('id')) == field_id), None)
            except ZOHO_ERRORS as exc:
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: re-read failed ({getattr(exc, "code", "")}).') from exc
            action['reread'] = self._summary(created)
            if created is None:
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: field {field_id} not found on re-read.')
            if created.get('api_name') != spec.api_name:
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: Zoho derived api_name {created.get("api_name")} '
                                          f'(spec {spec.api_name}, A1). Rename the label in the UI or update '
                                          'crm/schema_spec.py by PR, then re-plan (ZOHO-SETUP Z2).')
            problem = schema.compare(spec, created)
            if problem:
                raise _fail(EXIT_ABORTED, f'Aborted at {where}: the created field differs from the spec: {problem}.')
            action['outcome'] = 'created'
            self.stdout.write(self.style.SUCCESS(f'CREATED {item.api_name} (id {field_id}) in {item.module}'))

    @staticmethod
    def _created_id(body: dict | None) -> str:
        try:
            return zoho.result_id(body, 'POST', 'settings/fields')
        except zoho.ZohoPermanent:
            return ''

    @staticmethod
    def _summary(current: dict | None) -> dict | None:
        if current is None:
            return None
        return {
            'id': current.get('id'),
            'api_name': current.get('api_name'),
            'field_label': current.get('field_label'),
            'data_type': current.get('data_type'),
            'length': current.get('length'),
            'pick_list_values': [option.get('display_value') for option in current.get('pick_list_values') or []],
            'lookup_module': ((current.get('lookup') or {}).get('module') or {}).get('api_name'),
        }
