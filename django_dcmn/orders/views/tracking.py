# orders/views/tracking.py
"""Tracking views for CRM integration and public access."""

from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from ..models import Track
from ..serializers import TrackSerializer, PublicTrackSerializer
from ..constants import STAGE_DEFS, CRM_STAGE_MAP, ZOHO_MODULE_MAP
from ..utils import generate_tid, public_name, check_zoho_webhook_token
from ..tasks import write_tracking_id_to_zoho_task, send_tracking_email_task

import logging

logger = logging.getLogger(__name__)


class CreateTidFromCrmView(APIView):
    """Create tracking record from Zoho CRM webhook."""
    
    def post(self, request, format=None):
        if not check_zoho_webhook_token(request):
            return Response({'error': 'unauthorized'}, status=401)

        body = request.data
        node = body.get('data') if isinstance(body.get('data'), dict) else {}

        name = node.get('name') or body.get('name') or ''
        email = node.get('email') or body.get('email') or ''
        service = node.get('service') or body.get('service')
        # accept alias 'stage' for initial stage
        current_stage = (
            node.get('current_stage') or node.get('stage') or body.get('current_stage') or body.get('stage') or 'document_received'
        )

        # Alias for embassy -> embassy_legalization to support webhook JSON
        if service == 'embassy':
            service = 'embassy_legalization'

        # Alias for apostille -> state_apostille if not specified explicitly
        if service == 'apostille':
            service = 'state_apostille'
        
        comment = None  # do not include form comment on create
        zoho_module = node.get('zoho_module') or body.get('zoho_module')
        zoho_record_id = node.get('record_id') or body.get('record_id')

        if service not in STAGE_DEFS:
            return Response({'error': 'invalid service'}, status=400)

        codes = [d['code'] for d in STAGE_DEFS.get(service, [])]
        if current_stage not in codes:
            norm = str(current_stage or '').strip().lower()
            mapped = CRM_STAGE_MAP.get(service, {}).get(norm)
            current_stage = mapped if mapped in codes else 'document_received'

        tid = generate_tid()
        payload = {
            'name': name,
            'email': email,
            'service': service,
            'current_stage': current_stage,
        }
        # merge selected extra fields from data wrapper
        if node.get('shipping') is not None:
            payload['shipping'] = str(node.get('shipping'))
        if node.get('translation_r') is not None:
            tr_raw = str(node.get('translation_r')).strip().lower()
            payload['translation_r'] = True if ('translate' in tr_raw and 'yes' in tr_raw) or tr_raw in ('yes', 'true', '1') else False

        track = Track.objects.create(
            tid=tid,
            service=service,
            data=payload
        )

        # Write TID back to Zoho SYNCHRONOUSLY
        if zoho_module and zoho_record_id:
            # Save zoho_module/record_id in data for debugging
            try:
                d = track.data or {}
                d['zoho_module'] = zoho_module
                d['record_id'] = str(zoho_record_id)
                track.data = d
                track.save(update_fields=['data'])
            except Exception:
                pass

            # Send TID to Zoho CRM synchronously
            try:
                from ..zoho_sync import update_record_fields
                
                # Convert module name from webhook to API name
                api_module_name = ZOHO_MODULE_MAP.get(zoho_module, zoho_module)
                
                logger.info(f"[CreateTID] Attempting to write TID={tid} to Zoho {zoho_module} (API: {api_module_name})/{zoho_record_id}")
                success = update_record_fields(api_module_name, str(zoho_record_id), {"Tracking_ID": tid})
                
                if success:
                    logger.info(f"[CreateTID] ✅ Successfully wrote TID={tid} to Zoho")
                else:
                    logger.warning(f"[CreateTID] ⚠️ Failed to write TID={tid} to Zoho (returned False), enqueueing Celery task")
                    write_tracking_id_to_zoho_task.delay(api_module_name, zoho_record_id, tid)
            except Exception as e:
                logger.exception(f"[CreateTID] ❌ Exception writing TID={tid} to Zoho: {e}")
                # Fallback via Celery
                api_module_name = ZOHO_MODULE_MAP.get(zoho_module, zoho_module)
                write_tracking_id_to_zoho_task.delay(api_module_name, zoho_record_id, tid)

        # Send welcome email
        try:
            send_tracking_email_task.delay(tid, 'created')
        except Exception:
            logger.exception(f"[CreateTID] Failed to queue tracking email for TID={tid}")

        ser = TrackSerializer(track)
        return Response({'tid': tid, 'track': ser.data}, status=201)


class CrmUpdateStageView(APIView):
    """Update tracking stage from Zoho CRM webhook."""
    
    def post(self, request, format=None):
        if not check_zoho_webhook_token(request):
            return Response({'error': 'unauthorized'}, status=401)

        body = request.data
        node = body.get('data') if isinstance(body.get('data'), dict) else {}
        # accept aliases for tid
        tid = body.get('tid') or body.get('tracking_id') or body.get('Tracking_ID')
        if not tid:
            return Response({'error': 'tid required'}, status=400)

        track = Track.objects.filter(tid=tid).first()
        if not track:
            return Response({'error': 'not found'}, status=404)

        current_stage = body.get('current_stage') or node.get('current_stage')
        # accept alias 'stage' for crm_stage_name
        crm_stage_name = body.get('crm_stage_name') or body.get('stage') or node.get('crm_stage_name') or node.get('stage')
        comment = body.get('comment') or node.get('comment')

        track_data = track.data or {}
        service_key = track_data.get('service') or track.service

        # Save old stage to check for actual change
        old_stage = track_data.get('current_stage')
        stage_changed = False
        codes = [d['code'] for d in STAGE_DEFS.get(service_key, [])]
        
        if current_stage:
            if current_stage in codes:
                if old_stage != current_stage:
                    track_data['current_stage'] = current_stage
                    stage_changed = True
        elif crm_stage_name:
            norm = (crm_stage_name or '').strip().lower()
            mapped = CRM_STAGE_MAP.get(service_key, {}).get(norm)
            if mapped in codes:
                if old_stage != mapped:
                    track_data['current_stage'] = mapped
                    stage_changed = True

        if comment is not None:
            track_data['comment'] = str(comment)

        # passthrough additional fields from data and root (shipping, translation_r, etc.)
        for src in (node, body):
            try:
                for k, v in dict(src).items():
                    if k in ('tid', 'tracking_id', 'Tracking_ID', 'crm_stage_name', 'current_stage', 'stage', 'token', 'data'):
                        continue
                    track_data[k] = v
            except Exception:
                pass

        # normalize translation_r to boolean if present
        if 'translation_r' in track_data:
            tr_raw = str(track_data.get('translation_r')).strip().lower()
            track_data['translation_r'] = True if ('translate' in tr_raw and 'yes' in tr_raw) or tr_raw in ('yes', 'true', '1') else False

        track.data = track_data
        track.save(update_fields=['data', 'updated_at'])

        # Send email notification ONLY if stage actually changed
        try:
            if stage_changed and track_data.get('current_stage'):
                send_tracking_email_task.delay(track.tid, track_data.get('current_stage'))
                logger.info(f"📧 Stage changed for TID={track.tid}: {old_stage} → {track_data.get('current_stage')}")
        except Exception:
            logger.exception(f"Failed to queue tracking email for TID={track.tid}")
        
        return Response({'ok': True})


class PublicTrackView(APIView):
    """Public tracking page view."""
    permission_classes = [AllowAny]

    def get(self, request, tid: str):
        track = get_object_or_404(Track, tid=tid)
        ser = PublicTrackSerializer.from_track(track)
        payload = ser.data
        payload['name'] = public_name(payload.get('name', ''))
        return Response(payload)
