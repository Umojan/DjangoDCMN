"""Get a Quote v3 endpoints under /api/quote-requests/ (DESIGN §3, §5.1).

`QuoteSubmissionView.post` runs the checks of DESIGN §3.5 in table order and saves in <300 ms with no external
call except Turnstile (row 10, after both replay checks). Every answer is JSON, a 500 included.
"""

import json
import logging
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from crm.alerts import check_beat

from . import antispam, flags
from .contract import FALLBACK, SCHEMA_VERSION, UPLOAD_LIMITS
from .intake import (
    RequestMeta, attribution_from_body, confirmation_reply, replay, save_submission, soft_dedupe,
)
from .models import QuoteSubmission
from .validation import ContractError, normalise, parse_uuid

logger = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')
CONFIG_MAX_AGE_S = 60

__all__ = ['FALLBACK', 'QuoteConfigView', 'QuoteSubmissionView', 'QuoteUploadView']


class _PublicView(APIView):
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = ()   # the quote throttles run as row 3, after the kill switch and the size check


class QuoteSubmissionView(_PublicView):
    """POST /api/quote-requests/: JSON only, ≤64 KB."""
    http_method_names = ['post', 'options']
    parser_classes = ()     # the body is read and parsed here, after the size check (row 2)

    def post(self, request, format=None):
        try:
            return self._submit(request)
        except Exception:
            logger.exception('quote_submit_failed')
            return Response({'ok': False, 'detail': 'server_error', 'fallback': FALLBACK},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _submit(self, request):
        if not flags.quote_v3_enabled():                                          # row 1
            return _unavailable()
        raw = antispam.read_body(request)                                         # row 2
        if raw is None:
            return _verdict(antispam.too_large())
        if request.content_type.split(';')[0].strip().lower() != 'application/json':
            return Response({'ok': False, 'detail': 'unsupported_media_type'},
                            status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        try:
            body = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            return _bad_request(ContractError({'body': ['invalid']}))

        verdict = antispam.early_checks(request, body)                            # rows 3-5
        if verdict.action != 'allow':
            return _verdict(verdict)
        key = request.headers.get('Idempotency-Key')
        if key is not None and parse_uuid(key) != parse_uuid(body.get('submission_id')):
            return _bad_request(ContractError({'submission_id': ['mismatch']}))

        today = timezone.now().astimezone(ET).date()
        existing = antispam.replay_lookup(body)                                   # row 6
        if existing is not None:
            replay(existing, body, today=today)
            return _duplicate(existing)
        try:
            nq = normalise(body, today=today)                                     # row 7
        except ContractError as error:
            return _bad_request(error)
        earlier = soft_dedupe(nq)                                                 # row 8
        if earlier is not None:
            return _duplicate(earlier)
        verdict, turnstile_result = antispam.late_checks(request, nq, body)       # rows 9-11
        if verdict.action != 'allow':
            return _verdict(verdict)

        meta = RequestMeta(                                                       # rows 12-13 + save
            ip=antispam.client_ip(request),
            user_agent=request.headers.get('User-Agent', ''),
            turnstile_result=turnstile_result,
            attribution=attribution_from_body(body),
        )
        result = save_submission(nq, meta)
        if result.duplicate:
            return _duplicate(result.quote)
        quote = result.quote
        return Response({
            'ok': True,
            'reference': quote.reference,
            'confirmation_email': 'queued' if quote.confirmation_email_status == 'pending' else 'off',
            'received_at': antispam.received_at(quote.created_at),
        }, status=status.HTTP_201_CREATED)


class QuoteConfigView(_PublicView):
    """GET /api/quote-requests/config/: kill switch and limits for the page, cached 60 s.

    Disabled answers 503 with the same body and `enabled: false`, so the page shows the fallback either way.
    """
    http_method_names = ['get', 'options']

    def get(self, request, format=None):
        try:
            check_beat()
        except Exception:
            logger.exception('quote_config_beat_check_failed')
        enabled = flags.quote_v3_enabled()
        body = {
            'ok': enabled,
            'enabled': enabled,
            'test_only': flags.v3_test_only(),
            'schema_version': SCHEMA_VERSION,
            'turnstile_required': flags.turnstile_required(),
            'uploads': {'enabled': flags.uploads_enabled(), **UPLOAD_LIMITS},
            'fallback': FALLBACK,
        }
        if not enabled:
            body['detail'] = 'unavailable'
        response = Response(body, status=status.HTTP_200_OK if enabled else status.HTTP_503_SERVICE_UNAVAILABLE)
        response['Cache-Control'] = f'public, max-age={CONFIG_MAX_AGE_S}'
        return response


class QuoteUploadView(_PublicView):
    """POST /api/quote-requests/uploads/ (off until WP9)."""
    http_method_names = ['post', 'options']

    def post(self, request, format=None):
        return Response({'ok': False, 'detail': 'uploads_disabled'}, status=status.HTTP_404_NOT_FOUND)


def _reject_constant(name: str):
    """NaN and Infinity are not JSON (and Postgres jsonb refuses them)."""
    raise ValueError(f'invalid JSON constant {name}')


def _unavailable() -> Response:
    return Response(antispam.unavailable_body(), status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _bad_request(error: ContractError) -> Response:
    return Response(error.response_body(), status=error.status)


def _verdict(verdict: antispam.Verdict) -> Response:
    response = Response(verdict.body, status=verdict.status)
    if verdict.status == status.HTTP_429_TOO_MANY_REQUESTS:
        response['Retry-After'] = str(verdict.body['retry_after'])
    return response


def _duplicate(quote: QuoteSubmission) -> Response:
    return Response({
        'ok': True,
        'duplicate': True,
        'reference': quote.reference,
        'confirmation_email': confirmation_reply(quote),
        'received_at': antispam.received_at(quote.created_at),
    }, status=status.HTTP_200_OK)
