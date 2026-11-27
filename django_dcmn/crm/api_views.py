"""Engine HTTP API under /api/crm/v1/ (DESIGN §8.1–8.3), called by the Zoho buttons through the Connection
`dcmn_api` (header `X-DCMN-Key`).

Every body carries `message`, the sentence the Deluge function shows the manager (§7.14). The engine itself
audits each related-services call in `EngineRequest`; this module audits what it refuses before the engine
(a bad body, a caller outside its scope) and every minted panel link. Logs carry codes and ids only.
"""

import logging
import time

from django.core.cache import cache
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import (
    APIException, AuthenticationFailed, MethodNotAllowed, NotAuthenticated, ParseError, PermissionDenied,
    Throttled, UnsupportedMediaType,
)
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from quotes.antispam import client_ip
from quotes.models import QuoteSubmission
from quotes.render import ET

from . import engine, flags, messages, sources, zoho
from .alerts import alert_ops, check_beat
from .auth import ActionKeyAuthentication, CallerKeyThrottle, CallerScope
from .models import EngineRequest
from .panel.tokens import ACTION as PANEL_ACTION, TOKEN_MAX_AGE_S, make_panel_url
from .redact import redact_result
from .registry import NO_TARGET
from .sources import SourceNotAllowed, SourceNotFound, SourceRef
from .writeback import current_links

logger = logging.getLogger(__name__)

API_VERSION = 'rs.v1'
API_TRIGGERS = ('zoho_button', 'create_all')
API_MODES = ('create', 'preview', 'suggest')
SOURCE_READ_TIMEOUT: zoho.Timeout = (1.0, 4.0)   # one GET inside the 10 s Deluge limit
PANEL_LINKS_KEY = 'crm:panel:links:{hour}'
DONE_LINK_STATES = ('created', 'adopted')
RUNNING_JOB_STATES = ('accepted', 'running')

SCOPE_MESSAGE = 'This DCMN key may not do this. Nothing was created.'
THROTTLED_MESSAGE = 'Too many DCMN requests in the last minute. Nothing was created. Wait a moment and try again.'
BAD_REQUEST_MESSAGE = 'DCMN could not read this request. Nothing was created. Tell Vlad.'
INTERNAL_MESSAGE = 'DCMN hit an error. Nothing was duplicated. Try again in a minute; if it repeats, tell Vlad.'
JOB_NOT_FOUND_MESSAGE = 'This DCMN job was not found. Refresh the record and try again.'
BAD_ACTION_MESSAGE = 'The DCMN panel only adds services. Use Add service… again.'
PANEL_OPENING_MESSAGE = 'Opening the DCMN panel…'
OPTIONS_IGNORED_WARNING = 'options_ignored: options are accepted from the DCMN panels only'

# APIException → (detail, message) of the JSON body.
EXCEPTION_BODIES = (
    ((NotAuthenticated, AuthenticationFailed), 'unauthorized', messages.DISABLED),
    ((PermissionDenied,), 'scope', SCOPE_MESSAGE),
    ((Throttled,), 'throttled', THROTTLED_MESSAGE),
    ((ParseError, UnsupportedMediaType), 'bad_request', BAD_REQUEST_MESSAGE),
    ((MethodNotAllowed,), 'method_not_allowed', BAD_REQUEST_MESSAGE),
)


class Refusal(Exception):
    """A gate said no before the engine ran: answered as `{"ok": false, "detail": code, "message": …}`."""

    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code
        self.message = message

    def body(self) -> dict:
        return {'ok': False, 'detail': self.code, 'message': self.message}


# --- Shared with the panel ------------------------------------------------------------------------

def gate_source(module: str, record_id: str, *, timeout: zoho.Timeout = SOURCE_READ_TIMEOUT) -> SourceRef:
    """The §7.8 step 3 gates for a read of one source (engine off, module, circuit, test-only); one Zoho GET."""
    if flags.engine_mode() == 'off':
        raise Refusal(503, 'disabled', messages.DISABLED)
    try:
        sources.check_allowed(module)
    except SourceNotAllowed:
        raise Refusal(403, 'source_not_allowed', messages.SOURCE_NOT_ALLOWED) from None
    if zoho.circuit_is_open():
        raise Refusal(503, 'zoho_unavailable', messages.ZOHO_UNAVAILABLE)
    try:
        src = sources.resolve(module, record_id, timeout=timeout)
    except SourceNotFound:
        raise Refusal(404, 'source_not_found', messages.SOURCE_NOT_FOUND) from None
    except (zoho.ZohoTransient, zoho.ZohoPermanent):
        raise Refusal(503, 'zoho_unavailable', messages.ZOHO_UNAVAILABLE) from None
    if flags.engine_test_only() and not sources.is_test(src):
        raise Refusal(403, 'test_only', messages.TEST_ONLY)
    return src


def record_call(*, caller: str, mode: str, http_status: int, code: str, body: dict, caller_key_id: str = '',
                ip: str | None = None, trigger: str = '', source: SourceRef | None = None, module: str = '',
                record_id: str = '', actor: dict | None = None, services: list | None = None,
                started: float | None = None) -> EngineRequest:
    """Audit a finished call that `engine.handle` did not see: a refusal, a minted link, a dismissal."""
    return EngineRequest.objects.create(
        caller=caller[:12], trigger=trigger[:40], mode=mode[:8], actor=actor or {}, caller_key_id=caller_key_id[:8],
        ip=ip, source_key=source.key if source else '', source_module=(source.module if source else module)[:64],
        source_record_id=(source.record_id if source else record_id)[:32], services_requested=services or [],
        state='done' if http_status < 400 else 'refused', result=redact_result(body), http_status=http_status,
        error_code=code[:40], finished_at=timezone.now(),
        duration_ms=int((time.monotonic() - started) * 1000) if started is not None else None,
    )


def done_by(link) -> str:
    """`JD · button · 26 Nov 10:45` for a created or linked service; '' otherwise."""
    if link is None or link.state not in DONE_LINK_STATES:
        return ''
    actor = link.actor or {}
    who = actor.get('initials') or ('DCMN' if link.caller in ('system', 'rules') else '')
    when = (link.done_at or link.updated_at or timezone.now()).astimezone(ET)
    return ' · '.join(part for part in (who, messages.trigger_word(link.trigger), f'{when.day} {when:%b %H:%M}')
                      if part)


def source_lines(src: SourceRef) -> list[dict]:
    """Requested services first (in order), then any other service already linked to this source."""
    links = current_links(src.key)
    lines = []
    for key in dict.fromkeys([*src.requested, *links]):
        link = links.get(key)
        state = link.state if link else ('no_target' if key in NO_TARGET else 'requested')
        lines.append({
            'service': key, 'label': messages.label(key), 'requested': key in src.requested, 'state': state,
            'target_url': (link.target_url or None) if link else None, 'done_by': done_by(link),
            'candidates': list(link.candidates or []) if link and link.state == 'needs_decision' else [],
            'link_id': link.pk if link else None,
        })
    return lines


def lines_message(lines: list[dict]) -> str:
    active = [line for line in lines if line['state'] not in ('no_target', 'dismissed')]
    done = [line for line in active if line['state'] in DONE_LINK_STATES]
    return f'{len(done)} of {len(active)} services are in Zoho.' if active else messages.NOTHING_TO_DO


def _ref_label(source_key: str) -> str:
    if source_key.startswith('qr:') and source_key[3:].isdigit():
        return QuoteSubmission.objects.filter(pk=int(source_key[3:])).values_list('reference', flat=True).first() or ''
    return ''


def job_answer(job: EngineRequest) -> dict:
    """The polled state of a job; queued services show their link's current outcome (read-only)."""
    stored = dict(job.result or {})
    results = list(stored.get('results') or [])
    state, message = job.state, stored.get('message') or messages.IN_PROGRESS
    if job.state in RUNNING_JOB_STATES and job.source_key and results:
        links = current_links(job.source_key)
        results = [engine.link_result(links[r['service']]) if r.get('status') == 'queued' and r.get('service')
                   in links else r for r in results]
        state = engine.overall_state(results)
        message = messages.message_for(results, mode=job.mode, ref=job.ref, ref_label=_ref_label(job.source_key))
    return {'ok': (job.http_status or 200) < 400, 'job_id': str(job.public_id), 'ref': job.ref, 'state': state,
            'message': message, 'open_url': engine.open_url(results) if state != 'running' else None,
            'results': results}


# --- Request bodies -------------------------------------------------------------------------------

class _SourceField(serializers.Serializer):
    module = serializers.CharField(max_length=64)
    id = serializers.RegexField(r'^\d{1,32}$')


class _ActorField(serializers.Serializer):
    zoho_user_id = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.CharField(max_length=254, required=False, allow_blank=True)


class RelatedServicesRequest(serializers.Serializer):
    """Exactly what `crm/deluge/dcmn_create_service.dg` sends (DESIGN §8.2)."""

    api_version = serializers.ChoiceField(choices=[API_VERSION])
    source = _SourceField()
    services = serializers.ListField(child=serializers.CharField(max_length=24), min_length=1, max_length=20)
    mode = serializers.ChoiceField(choices=API_MODES, default='create')
    trigger = serializers.ChoiceField(choices=API_TRIGGERS, required=False)
    actor = _ActorField(required=False)
    client_request_id = serializers.CharField(max_length=140, required=False, allow_blank=True)
    options = serializers.DictField(required=False)

    def validate(self, data):
        data.setdefault('trigger', 'create_all' if data['services'] == ['*'] else 'zoho_button')
        return data


class PanelLinkRequest(serializers.Serializer):
    """Exactly what `crm/deluge/dcmn_open_panel.dg` sends (DESIGN §8.3)."""

    source = _SourceField()
    actor = _ActorField(required=False)
    action = serializers.CharField(max_length=40)


# --- Views ----------------------------------------------------------------------------------------

class EngineAPIView(APIView):
    """Key auth, caller scope, `crm_key` throttle; every error body is `{ok, detail, message}`."""

    authentication_classes = [ActionKeyAuthentication]
    permission_classes = [CallerScope]
    throttle_classes = [CallerKeyThrottle]
    parser_classes = [JSONParser]
    allowed_callers: dict[str, frozenset[str]] = {}

    def initial(self, request, *args, **kwargs):
        try:
            check_beat()   # one of the three places that notice a stopped beat (DESIGN §12.1)
        except Exception:
            logger.exception('crm_api_beat_check_failed')
        super().initial(request, *args, **kwargs)

    def permission_denied(self, request, message=None, code=None):
        if request.successful_authenticator is not None:   # a known key outside its scope
            body = {'ok': False, 'detail': 'scope', 'message': SCOPE_MESSAGE}
            self.audit(request, http_status=403, code='scope', body=body)
            logger.warning('crm_scope_refused caller=%s method=%s view=%s', request.user, request.method,
                           type(self).__name__)
        super().permission_denied(request, message=message, code=code)

    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        detail, message = 'error', INTERNAL_MESSAGE
        for classes, code, text in EXCEPTION_BODIES:
            if isinstance(exc, classes):
                detail, message = code, text
                break
        else:
            if isinstance(exc, APIException) and response.status_code == 404:
                detail, message = 'not_found', JOB_NOT_FOUND_MESSAGE
        response.data = {'ok': False, 'detail': detail, 'message': message}
        return response

    def caller(self, request) -> str:
        return getattr(request.user, 'caller', '')

    def actor(self, request, data: dict) -> dict:
        actor = data.get('actor') or {}
        return {'kind': f'{self.caller(request)}_user', 'zoho_user_id': actor.get('zoho_user_id', ''),
                'email': actor.get('email', '')}

    def audit(self, request, *, http_status: int, code: str, body: dict, mode: str = '', **fields) -> EngineRequest:
        """An EngineRequest for a call the engine did not see (invariant 10)."""
        return record_call(caller=self.caller(request), caller_key_id=str(request.auth or ''), ip=client_ip(request),
                           mode=mode or request.method.lower(), http_status=http_status, code=code, body=body,
                           **fields)


def _bad_request(errors) -> dict:
    return {'ok': False, 'detail': 'bad_request', 'errors': errors, 'message': BAD_REQUEST_MESSAGE}


class RelatedServicesView(EngineAPIView):
    """POST create/preview/suggest (the engine); GET status ?module=&id= (lines of one source)."""

    http_method_names = ['get', 'post']
    allowed_callers = {'POST': frozenset({'zoho'}), 'GET': frozenset({'zoho', 'gmail'})}

    def post(self, request, format=None):
        body = RelatedServicesRequest(data=request.data if isinstance(request.data, dict) else {})
        if not body.is_valid():
            answer = _bad_request(body.errors)
            self.audit(request, http_status=400, code='bad_request', body=answer, mode='create')
            return Response(answer, status=status.HTTP_400_BAD_REQUEST)
        data = body.validated_data
        warnings = []
        if data.get('options'):
            warnings.append(OPTIONS_IGNORED_WARNING)
            logger.warning('crm_options_ignored caller=%s', self.caller(request))
        try:
            result = engine.handle(
                (data['source']['module'], data['source']['id']), data['services'], mode=data['mode'],
                trigger=data['trigger'], caller=self.caller(request), actor=self.actor(request, data),
                client_request_id=data.get('client_request_id') or None, ip=client_ip(request),
                caller_key_id=str(request.auth or ''),
            )
        except Exception:   # the engine has already saved its EngineRequest as failed
            logger.exception('crm_related_services_failed')
            return Response({'ok': False, 'detail': 'internal', 'message': INTERNAL_MESSAGE},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        answer = result.body
        if warnings:
            answer = {**answer, 'warnings': [*answer.get('warnings', []), *warnings]}
            EngineRequest.objects.filter(pk=result.request.pk).update(result=redact_result(answer))
        return Response(answer, status=result.http_status)

    def get(self, request, format=None):
        source = _SourceField(data={'module': request.query_params.get('module', ''),
                                    'id': request.query_params.get('id', '')})
        if not source.is_valid():
            return Response(_bad_request(source.errors), status=status.HTTP_400_BAD_REQUEST)
        try:
            src = gate_source(source.validated_data['module'], source.validated_data['id'])
        except Refusal as refusal:
            return Response(refusal.body(), status=refusal.http_status)
        lines = source_lines(src)
        return Response({'ok': True, 'source': {'kind': src.kind, 'key': src.key, 'ref_label': src.ref_label},
                         'lines': lines, 'message': lines_message(lines)})


class JobView(EngineAPIView):
    """GET jobs/<uuid:job_id>/: the state of one engine call (polled every 2 s for up to 60 s)."""

    http_method_names = ['get']
    allowed_callers = {'GET': frozenset({'zoho'})}

    def get(self, request, job_id, format=None):
        job = EngineRequest.objects.filter(public_id=job_id).first()
        if job is None:
            return Response({'ok': False, 'detail': 'not_found', 'message': JOB_NOT_FOUND_MESSAGE},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(job_answer(job))


class PanelLinkView(EngineAPIView):
    """POST panel-links/: a single-use 900 s link to the "Add service…" panel (action `add_service` only)."""

    http_method_names = ['post']
    allowed_callers = {'POST': frozenset({'zoho', 'gmail'})}

    def post(self, request, format=None):
        started = time.monotonic()
        body = PanelLinkRequest(data=request.data if isinstance(request.data, dict) else {})
        audit = {'mode': 'panel', 'trigger': 'panel_link', 'started': started}
        if not body.is_valid():
            answer = _bad_request(body.errors)
            self.audit(request, http_status=400, code='bad_request', body=answer, **audit)
            return Response(answer, status=status.HTTP_400_BAD_REQUEST)
        data = body.validated_data
        module, record_id = data['source']['module'], data['source']['id']
        actor = self.actor(request, data)
        audit.update(module=module, record_id=record_id, actor=actor)
        if data['action'] != PANEL_ACTION:
            answer = {'ok': False, 'detail': 'bad_action', 'message': BAD_ACTION_MESSAGE}
            self.audit(request, http_status=400, code='bad_action', body=answer, **audit)
            return Response(answer, status=status.HTTP_400_BAD_REQUEST)
        try:
            src = gate_source(module, record_id)
        except Refusal as refusal:
            self.audit(request, http_status=refusal.http_status, code=refusal.code, body=refusal.body(), **audit)
            return Response(refusal.body(), status=refusal.http_status)
        url = make_panel_url({'module': module, 'id': record_id}, actor)
        answer = {'ok': True, 'url': url, 'expires_in': TOKEN_MAX_AGE_S, 'message': PANEL_OPENING_MESSAGE}
        # The stored copy never holds the token: it would open the panel for 15 minutes.
        self.audit(request, http_status=200, code='', body={**answer, 'url': '<minted>'}, source=src, **audit)
        _count_panel_link()
        return Response(answer)


def _count_panel_link() -> None:
    """`panel_link_spike` when more than CRM_PANEL_LINK_ALERT_HOUR links are minted in one clock hour."""
    hour = int(time.time() // 3600)
    key = PANEL_LINKS_KEY.format(hour=hour)
    if cache.add(key, 1, 3600 + 60):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.add(key, 1, 3600 + 60)
            count = 1
    limit = flags.panel_link_alert_hour()
    if count > limit:
        logger.warning('panel_link_spike count=%s limit=%s', count, limit)
        alert_ops('panel_link_spike', str(hour), 'Many DCMN panel links minted',
                  f'{count} panel links were minted this hour (alert threshold {limit}). Links are still minted. '
                  'Check EngineRequest rows with trigger=panel_link by caller_key_id; rotate the key '
                  '(ZOHO-SETUP Z6) if the clicks are not the managers\'.')
