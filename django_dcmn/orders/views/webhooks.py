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
    
    URL: /api/orders/webhook/whatconverts-test/
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
