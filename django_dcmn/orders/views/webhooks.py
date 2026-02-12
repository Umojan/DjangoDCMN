# orders/views/webhooks.py
"""External webhook handlers (WhatConverts, etc.)"""

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse

import json
import logging

logger = logging.getLogger(__name__)


@csrf_exempt
def whatconverts_test_webhook(request):
    """
    Test endpoint to receive and log WhatConverts webhook data.
    This is for testing purposes only - logs all incoming data.

    URL: /api/webhook/whatconverts-test/
    """
    # Collect all data
    webhook_data = {
        'method': request.method,
        'content_type': request.content_type,
        'headers': dict(request.headers),
        'GET_params': dict(request.GET),
        'POST_params': dict(request.POST),
        'body_raw': None,
        'body_json': None,
    }

    # Try to get raw body
    try:
        webhook_data['body_raw'] = request.body.decode('utf-8')
    except Exception as e:
        webhook_data['body_raw'] = f"Error decoding body: {e}"

    # Try to parse as JSON
    try:
        webhook_data['body_json'] = json.loads(request.body)
    except Exception:
        webhook_data['body_json'] = None

    # Log everything
    logger.info("=" * 60)
    logger.info("📞 WhatConverts Test Webhook Received")
    logger.info("=" * 60)
    logger.info(f"Method: {webhook_data['method']}")
    logger.info(f"Content-Type: {webhook_data['content_type']}")
    logger.info(f"Headers: {json.dumps(webhook_data['headers'], indent=2, default=str)}")
    logger.info(f"GET params: {webhook_data['GET_params']}")
    logger.info(f"POST params: {webhook_data['POST_params']}")
    logger.info(f"Body (raw): {webhook_data['body_raw']}")
    logger.info(f"Body (JSON): {json.dumps(webhook_data['body_json'], indent=2, default=str) if webhook_data['body_json'] else 'N/A'}")
    logger.info("=" * 60)

    # Return all data for easy viewing
    return JsonResponse({
        'status': 'received',
        'message': 'WhatConverts test webhook data logged successfully',
        'received_data': webhook_data
    })


@csrf_exempt
def whatconverts_webhook(request):
    """
    Production WhatConverts webhook handler.

    Processes phone call leads from WhatConverts:
    1. Filters for "Phone Call" lead type only
    2. Ignores tracking page leads
    3. Detects service from landing URL
    4. Checks for duplicate leads
    5. Creates PhoneCallLead in Django
    6. Syncs to Zoho with "Phone Call Received" stage
    7. Checks for matching web form orders

    URL: /api/webhook/whatconverts/
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST requests allowed'}, status=405)

    try:
        # Parse webhook data
        data = json.loads(request.body)

        logger.info("=" * 80)
        logger.info("📞 WhatConverts Webhook Received")
        logger.info(f"   Lead ID: {data.get('lead_id')}")
        logger.info(f"   Lead Type: {data.get('lead_type')}")
        logger.info(f"   Landing URL: {data.get('landing_url')}")
        logger.info("=" * 80)

        # Filter 1: Only accept "Phone Call" leads
        if data.get('lead_type') != 'Phone Call':
            logger.info(f"⏭️ Skipping non-phone lead: {data.get('lead_type')}")
            return JsonResponse({
                'status': 'skipped',
                'reason': 'Not a phone call lead'
            })

        # Filter 2: Ignore tracking page leads
        landing_url = data.get('landing_url', '')
        if '/tracking' in landing_url.lower():
            logger.info(f"⏭️ Skipping tracking page lead: {landing_url}")
            return JsonResponse({
                'status': 'skipped',
                'reason': 'Tracking page lead ignored'
            })

        # Filter 3: Check for spam
        if data.get('spam'):
            logger.info(f"🚫 Skipping spam lead")
            return JsonResponse({
                'status': 'skipped',
                'reason': 'Marked as spam'
            })

        # Process the phone lead
        from ..services.whatconverts import process_whatconverts_phone_lead
        from ..services.whatconverts_zoho import sync_phone_lead_to_zoho

        phone_lead = process_whatconverts_phone_lead(data)

        # None = matching order exists, phone lead intentionally skipped
        if phone_lead is None:
            logger.info("✅ Phone lead skipped (matching order exists)")
            return JsonResponse({
                'status': 'skipped',
                'reason': 'Matching order already exists',
                'message': '90% probability: clarification call about existing order'
            })

        # Sync to Zoho
        success = sync_phone_lead_to_zoho(phone_lead)

        if success:
            logger.info(f"✅ Successfully processed and synced phone lead {phone_lead.id}")

            return JsonResponse({
                'status': 'success',
                'phone_lead_id': phone_lead.id,
                'zoho_lead_id': phone_lead.zoho_lead_id,
                'zoho_attribution_id': phone_lead.zoho_attribution_id,
                'detected_service': phone_lead.detected_service,
                'matched_with_form': phone_lead.matched_with_form,
            })
        else:
            logger.warning(f"⚠️ Phone lead {phone_lead.id} created but Zoho sync failed")
            return JsonResponse({
                'status': 'partial',
                'message': 'Lead saved but Zoho sync failed',
                'phone_lead_id': phone_lead.id,
            })

    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON: {e}")
        return JsonResponse({
            'status': 'error',
            'message': 'Invalid JSON payload'
        }, status=400)

    except Exception as e:
        logger.error(f"❌ Webhook processing error: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': 'Internal processing error'
        }, status=500)
