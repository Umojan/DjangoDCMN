"""The "Add service…" panel (DESIGN §8.4, §9.4): Django templates, no SPA.

- Token panel (`caller="panel"`): `GET /crm/panel/?t=…` checks and claims the minted link, sets the
  `crm_panel` cookie and renders the page directly (no redirect). Later requests use the cookie only:
  `GET /crm/panel/` re-renders, `GET /crm/panel/?job=<id>` polls a job, `POST /crm/panel/act/` acts
  (CSRF, initials required). Options are limited to the token subset (`crm.panel.options`).
- Plan B staff panel (`caller="staff"`): `GET|POST /crm/panel/zoho/?module=&id=` behind a Django login in group
  `crm_panel`; every option, and the actor is the person (`staff_user_id`).

Every engine call goes through `crm.engine.handle`; the panel audits only what it refuses or does itself.
"""

import logging
import re
import uuid
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from rest_framework.throttling import SimpleRateThrottle

from quotes.antispam import client_ip

from .. import engine, flags, messages
from ..api_views import Refusal, gate_source, job_answer, record_call, source_lines
from ..facts import SOURCE_FIELD_MAPS
from ..models import EngineRequest, ServiceLink
from ..registry import AUTOMATED_MARK, REGISTRY
from .forms import PanelActionForm
from .options import OptionNotAllowed, check_options
from .tokens import SignatureExpired, TokenReused, BadSignature, make_panel_ref, panel_ref_matches, \
    read_panel_cookie, read_panel_token, set_panel_cookie

logger = logging.getLogger(__name__)

__all__ = ['panel_act', 'panel_open', 'panel_zoho']

STAFF_GROUP = 'crm_panel'
PANEL_READ_TIMEOUT = (3, 8)
POLL_EVERY_MS = 2000
POLL_FOR_MS = 60000
MODULE = re.compile(r'^\w{1,64}$')
RECORD_ID = re.compile(r'^\d{1,32}$')
ADDABLE = tuple(key for key in REGISTRY if key != 'notary')   # the alias expands; the concrete types are listed

EXPIRED = 'This panel link has expired (links work for 15 minutes). Click Add service… in Zoho again.'
USED = 'This panel link was already used or is not valid. Click Add service… in Zoho again.'
OPEN_FROM_ZOHO = 'Open the panel from the Add service… button in Zoho.'
INITIALS_REQUIRED = 'Type your initials (2–4 letters) before each action. Nothing was created.'
SIGN_IN = 'Sign in with your own DCMN login to use the staff panel.'
NOT_IN_GROUP = 'Your DCMN login may not use the staff panel. Ask Vlad to add you to the crm_panel group.'
BAD_SOURCE = 'This link does not name a Zoho record. Use the DCMN staff panel button on the record.'
NOT_SUGGESTED = 'Only a suggestion can be dismissed. Nothing was changed.'
THROTTLED = 'Too many clicks in the last minute. Nothing was created. Wait a moment and try again.'
JOB_NOT_FOUND = 'This job was not found for this record.'
STALE_PANEL = ('This panel window is out of date: another Add service… panel was opened after it. Nothing was '
               'created. Close it and click Add service… again.')
INTERNAL = 'DCMN hit an error. Nothing was duplicated. Try again in a minute; if it repeats, tell Vlad.'
STAFF_HINT = ' The DCMN staff panel can do this.'
STATE_WORDS = {
    'requested': 'not in Zoho yet', 'no_target': 'no record needed', 'pending': 'queued', 'creating': 'creating',
    'retry': 'retrying', 'created': 'in Zoho', 'adopted': 'linked', 'failed': 'failed', 'degraded': 'needs a fix',
    'target_deleted': 'record deleted', 'dismissed': 'dismissed', 'suggested': 'suggested',
    'needs_decision': 'pick a record',
}
CREATABLE = ('requested', 'failed', 'degraded', 'dismissed')


class PanelThrottle(SimpleRateThrottle):
    """`crm_panel`: 60/min per panel session (token panel) or per staff user."""

    scope = 'crm_panel'

    def __init__(self, ident: str):
        super().__init__()
        self.ident = ident

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.ident}


# --- Token panel ----------------------------------------------------------------------------------

@require_GET
def panel_open(request):
    """GET /crm/panel/?t=<token> opens; GET /crm/panel/ re-renders and ?job=<id> polls (cookie)."""
    if flags.engine_mode() == 'off':
        return _closed(request, messages.DISABLED, 503)
    token = request.GET.get('t')
    if token:
        try:
            payload = read_panel_token(token)
        except SignatureExpired:
            return _closed(request, EXPIRED, 403)
        except (TokenReused, BadSignature):
            return _closed(request, USED, 403)
        previous = read_panel_cookie(request) or {}
        state = {'m': payload['m'], 'r': payload['r'], 'a': payload.get('a') or {}, 'sid': payload['jti'],
                 'initials': previous.get('initials', '')}
        response = _panel_page(request, state, caller='panel', strip_token=True)
        if response.status_code == 200:
            set_panel_cookie(response, state)
        return response
    state = read_panel_cookie(request)
    if state is None:
        return _closed(request, OPEN_FROM_ZOHO, 403)
    if not _allow(request, f'panel:{state.get("sid", "")}'):
        return _closed(request, THROTTLED, 429)
    if 'job' in request.GET:
        if not panel_ref_matches(request.GET.get('ref'), state):
            return JsonResponse({'ok': False, 'detail': 'stale_panel', 'state': 'refused', 'message': STALE_PANEL},
                                status=409)
        return _job(request, state['m'], state['r'])
    return _panel_page(request, state, caller='panel')


@require_POST
def panel_act(request):
    """POST /crm/panel/act/ (CSRF; initials required; caller="panel")."""
    if flags.engine_mode() == 'off':
        return _closed(request, messages.DISABLED, 503)
    state = read_panel_cookie(request)
    if state is None:
        return _closed(request, OPEN_FROM_ZOHO, 403)
    if not _allow(request, f'panel:{state.get("sid", "")}'):
        return _closed(request, THROTTLED, 429)
    if not panel_ref_matches(request.POST.get('panel_ref'), state):
        # The window shows another record than the cookie now names: acting would hit the wrong client.
        logger.warning('crm_panel_stale_window sid=%s', state.get('sid', ''))
        return _closed(request, STALE_PANEL, 409)
    actor = {'kind': 'panel', 'zoho_user_id': str((state.get('a') or {}).get('zoho_user_id') or '')}
    response, initials = _act(request, state, caller='panel', actor=actor)
    if initials and initials != state.get('initials'):
        set_panel_cookie(response, {**state, 'initials': initials})   # remembered for the next action
    return response


# --- Plan B staff panel ---------------------------------------------------------------------------

@require_http_methods(['GET', 'POST'])
def panel_zoho(request):
    """GET|POST /crm/panel/zoho/?module=&id= (Plan B; Django login in group crm_panel; caller="staff")."""
    if flags.engine_mode() == 'off':
        return _closed(request, messages.DISABLED, 503)
    user = request.user
    if not user.is_authenticated:
        login = f'{reverse("admin:login")}?{urlencode({"next": request.get_full_path()})}'
        return _closed(request, SIGN_IN, 403, login_url=login)
    if not (user.is_active and user.is_staff and user.groups.filter(name=STAFF_GROUP).exists()):
        return _closed(request, NOT_IN_GROUP, 403)
    module, record_id = request.GET.get('module', ''), request.GET.get('id', '')
    if not (MODULE.match(module) and RECORD_ID.match(record_id)):
        return _closed(request, BAD_SOURCE, 400)
    if not _allow(request, f'staff:{user.pk}'):
        return _closed(request, THROTTLED, 429)
    state = {'m': module, 'r': record_id}
    if request.method == 'POST':
        actor = {'kind': 'staff', 'staff_user_id': user.pk, 'email': user.email or ''}
        return _act(request, state, caller='staff', actor=actor)[0]
    if 'job' in request.GET:
        return _job(request, module, record_id)
    return _panel_page(request, state, caller='staff')


# --- Pages ----------------------------------------------------------------------------------------

def _allow(request, ident: str) -> bool:
    return PanelThrottle(ident).allow_request(request, None)


def _closed(request, message: str, status: int, *, login_url: str = ''):
    return render(request, 'crm/panel/login_required.html', {'message': message, 'login_url': login_url},
                  status=status)


def _urls(state: dict, caller: str) -> dict:
    if caller == 'staff':
        page = f'{reverse("crm_panel_zoho")}?{urlencode({"module": state["m"], "id": state["r"]})}'
        return {'page': page, 'act': page, 'poll': f'{page}&job='}
    ref = make_panel_ref(state)
    return {'page': reverse('crm_panel_open'), 'act': reverse('crm_panel_act'), 'ref': ref,
            'poll': f'{reverse("crm_panel_open")}?{urlencode({"ref": ref})}&job='}


def _header(src) -> dict:
    if src.kind == 'quote' and src.quote is not None:
        quote_ = src.quote
        return {'name': quote_.full_name, 'email': quote_.email, 'phone': quote_.phone_raw,
                'ref': quote_.reference, 'url': quote_.zoho_url}
    record, fields = src.record or {}, SOURCE_FIELD_MAPS.get(src.module, {})
    return {'name': record.get(fields.get('client_name', '')) or '', 'email': record.get(fields.get('email', '')) or '',
            'phone': record.get(fields.get('phone', '')) or '', 'ref': src.ref_label, 'url': src.url}


def _line_actions(line: dict, staff: bool) -> list[tuple[str, str, bool]]:
    """(op, button word, primary) for one requested or linked line."""
    service, line_state = line['service'], line['state']
    if line_state == 'no_target':
        return []
    actions = []
    if line_state in CREATABLE:
        actions += [(f'preview:{service}', 'Preview', False), (f'create:{service}', 'Create', True)]
    elif line_state == 'target_deleted':
        actions.append((f'create_again:{service}', 'Create again', True))
    elif staff and line_state in ('created', 'adopted'):
        actions.append((f'create_again:{service}', 'Create again', False))
    if staff:
        actions.append((f'link:{service}', 'Link id', False))
    return actions


def _panel_page(request, state: dict, *, caller: str, strip_token: bool = False):
    try:
        src = gate_source(state['m'], state['r'], timeout=PANEL_READ_TIMEOUT)
    except Refusal as refusal:
        return _closed(request, refusal.message, refusal.http_status)
    lines = source_lines(src)
    for line in lines:
        line['state_word'] = STATE_WORDS.get(line['state'], line['state'])
        line['actions'] = _line_actions(line, caller == 'staff')
    listed = {line['service'] for line in lines}
    context = {
        'caller': caller, 'staff': caller == 'staff', 'urls': _urls(state, caller), 'header': _header(src),
        'requested': [line for line in lines if line['state'] not in ('suggested', 'needs_decision')],
        'suggested': [line for line in lines if line['state'] == 'suggested'],
        'decisions': [line for line in lines if line['state'] == 'needs_decision'],
        'addable': [(key, REGISTRY[key].label) for key in ADDABLE if key not in listed],
        'initials': state.get('initials', ''), 'strip_token': strip_token, 'preselect': request.GET.get('svc', ''),
    }
    return render(request, 'crm/panel/panel.html', context)


def _result_page(request, state: dict, caller: str, body: dict, status: int, *, initials: str = ''):
    results = list(body.get('results') or [])
    for result in results:
        spec = REGISTRY.get(result.get('service', ''))
        result['label'] = messages.label(result.get('service', ''))
        result['stages'] = [stage for stage in spec.stages() if AUTOMATED_MARK not in stage] if spec else []
    urls = _urls(state, caller)
    running = body.get('state') == 'running' and body.get('job_id')
    context = {
        'caller': caller, 'staff': caller == 'staff', 'urls': urls, 'body': body, 'results': results,
        'initials': initials, 'labels': {key: spec.label for key, spec in REGISTRY.items()},
        'poll': {'url': f'{urls["poll"]}{body["job_id"]}', 'every_ms': POLL_EVERY_MS, 'for_ms': POLL_FOR_MS}
        if running else None,
    }
    return render(request, 'crm/panel/result.html', context, status=status)


def _job(request, module: str, record_id: str):
    try:
        job_id = uuid.UUID(request.GET.get('job', ''))
    except ValueError:
        job_id = None
    job = EngineRequest.objects.filter(public_id=job_id, source_module=module, source_record_id=record_id).first() \
        if job_id else None
    if job is None:
        return JsonResponse({'ok': False, 'detail': 'not_found', 'message': JOB_NOT_FOUND}, status=404)
    return JsonResponse(job_answer(job))


# --- Actions --------------------------------------------------------------------------------------

def _latest_link(module: str, record_id: str, service_key: str) -> ServiceLink | None:
    """The current link of a service on this record (a v5 quote's links may predate its GAQL id)."""
    same_record = Q(source_record_id=record_id) | Q(source_kind='quote', quote__zoho_record_id=record_id)
    return (ServiceLink.objects.filter(same_record, source_module=module, service_key=service_key)
            .order_by('-seq').first())


def _act(request, state: dict, *, caller: str, actor: dict):
    """Run one panel action; returns (response, initials)."""
    form = PanelActionForm(request.POST)
    if not form.is_valid():
        errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
        body = {'ok': False, 'detail': 'bad_request', 'message': ' '.join(errors) or 'Unknown action.'}
        return _result_page(request, state, caller, body, 400), ''
    data = form.cleaned_data
    initials = (data.get('initials') or '').upper()
    if caller == 'panel' and not initials:
        body = {'ok': False, 'detail': 'initials_required', 'message': INITIALS_REQUIRED}
        return _result_page(request, state, caller, body, 400), ''
    if initials:
        actor = {**actor, 'initials': initials}
    module, record_id, services = state['m'], state['r'], data['service_keys']
    call = {'caller': caller, 'actor': actor, 'module': module, 'record_id': record_id, 'services': services,
            'ip': client_ip(request), 'trigger': caller}

    if data['action'] == 'dismiss':
        body, status = _dismiss(services[0], call)
        return _result_page(request, state, caller, body, status, initials=initials), initials

    options = form.engine_options()
    link = _latest_link(module, record_id, services[0]) if len(services) == 1 else None
    try:
        for key in services:
            check_options(caller, options, link, service_key=key)
    except OptionNotAllowed as exc:
        hint = STAFF_HINT if caller == 'panel' else ''
        body = {'ok': False, 'detail': exc.code, 'option': exc.option,
                'message': f'Not allowed here: {exc.option} ({exc.reason}). Nothing was created.{hint}'}
        record_call(mode=form.mode(), http_status=403, code=exc.code, body=body, **call)
        logger.warning('crm_panel_option_refused caller=%s option=%s', caller, exc.option)
        return _result_page(request, state, caller, body, 403, initials=initials), initials

    try:
        result = engine.handle((module, record_id), services, mode=form.mode(), trigger=caller, caller=caller,
                               actor=actor, options=options, ip=call['ip'])
    except Exception:   # the engine has already saved its EngineRequest as failed
        logger.exception('crm_panel_action_failed caller=%s', caller)
        body = {'ok': False, 'detail': 'internal', 'message': INTERNAL}
        return _result_page(request, state, caller, body, 500, initials=initials), initials
    return _result_page(request, state, caller, result.body, result.http_status, initials=initials), initials


def _dismiss(service_key: str, call: dict) -> tuple[dict, int]:
    """A suggestion → dismissed (no Zoho record; the write-back updates the quote lines)."""
    link = _latest_link(call['module'], call['record_id'], service_key)
    if link is None or link.state != 'suggested':
        body = {'ok': False, 'detail': 'not_suggested', 'message': NOT_SUGGESTED}
        record_call(mode='dismiss', http_status=409, code='not_suggested', body=body, **call)
        return body, 409
    body = {'ok': True, 'state': 'done', 'message': f'{messages.label(service_key)} suggestion dismissed. '
                                                  'Nothing was created in Zoho.',
            'results': [{'service': service_key, 'status': 'dismissed', 'link_id': link.pk}]}
    audit = record_call(mode='dismiss', http_status=200, code='', body=body, **call)
    ServiceLink.objects.filter(pk=link.pk, state='suggested').update(
        state='dismissed', request=audit, actor=call['actor'], caller=call['caller'], trigger=call['trigger'],
        updated_at=timezone.now())
    from ..tasks.writeback import writeback_source

    transaction.on_commit(lambda: writeback_source.delay(link.source_key))
    return body, 200
