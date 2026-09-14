"""
Public endpoints for the B2B site forms:

    POST /api/business-accounts/apply/   (JSON)
    POST /api/partners/apply/            (JSON)

Response contract (the Webflow bridge dcmn-apply.js depends on it):
    2xx → {"ok": true, "id": <application id>}
    4xx → {"detail": "<human readable message>"}   (400 validation, 429 throttle)

The application is ALWAYS saved and 200 returned even if Zoho / email fail;
Zoho sync goes through the durable outbox (ZohoSyncJob) exactly like the
other forms, the manager email is sent synchronously with a try/except.
"""

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from ..models import Application
from ..serializers import ApplicationSerializer
from ..services.attribution import get_client_ip, process_attribution
from ..services.applications import send_application_notification, verify_turnstile
from ..tasks import enqueue_zoho_sync

logger = logging.getLogger(__name__)

# Fields that do not exist on the real forms. Bots that fill them get a fake OK.
HONEYPOT_FIELDS = ('website_confirm', 'fax')


class ApplicationBurstThrottle(ScopedRateThrottle):
    scope = 'applications_burst'


class ApplicationSustainedThrottle(SimpleRateThrottle):
    scope = 'applications_sustained'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class BaseApplicationView(APIView):
    program = None  # set in subclasses
    throttle_classes = [ApplicationBurstThrottle, ApplicationSustainedThrottle]
    throttle_scope = 'applications_burst'
    http_method_names = ['post', 'options']

    def post(self, request, format=None):
        data = request.data if isinstance(request.data, dict) else {}

        # Honeypot: silently accept and drop.
        for field in HONEYPOT_FIELDS:
            if str(data.get(field) or '').strip():
                logger.warning("Honeypot hit on %s application (field=%s) from %s", self.program, field, get_client_ip(request))
                return Response({'ok': True, 'id': None}, status=status.HTTP_200_OK)

        serializer = ApplicationSerializer(data=data, program=self.program)
        if not serializer.is_valid():
            message = ApplicationSerializer.first_error_message(serializer.errors)
            return Response({'detail': message}, status=status.HTTP_400_BAD_REQUEST)

        # Cloudflare Turnstile (only enforced when TURNSTILE_SECRET_KEY is configured)
        turnstile_ok = verify_turnstile(serializer.validated_data.get('turnstile_token'), get_client_ip(request))
        if turnstile_ok is False:
            return Response(
                {'detail': 'Verification failed, please reload the page and try again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fields = serializer.to_application_fields()
        fields['ip'] = get_client_ip(request) or None
        fields['user_agent'] = (request.META.get('HTTP_USER_AGENT') or '')[:500]
        fields['raw_payload'] = _json_safe(data)
        app = Application.objects.create(**fields)

        # Attribution (DCMNTracker payload in request.data.attribution)
        try:
            process_attribution(request, app)
        except Exception:
            logger.exception("Attribution processing failed for application %s", app.id)

        # Zoho: durable outbox + Celery (same path as every other form)
        try:
            enqueue_zoho_sync(app.id, 'application')
        except Exception:
            logger.exception("Failed to enqueue Zoho sync for application %s", app.id)

        # Managers email
        send_application_notification(app)

        return Response({'ok': True, 'id': app.id}, status=status.HTTP_200_OK)


class BusinessAccountApplyView(BaseApplicationView):
    """Business Account Application form (/business-accounts)."""
    program = Application.PROGRAM_BUSINESS_ACCOUNT


class PartnerApplyView(BaseApplicationView):
    """Partner Application form (/partners)."""
    program = Application.PROGRAM_PARTNER


def _json_safe(data):
    """Keep only JSON-serialisable primitives from the incoming payload."""
    import json
    try:
        return json.loads(json.dumps(data, default=str))
    except Exception:
        return None
