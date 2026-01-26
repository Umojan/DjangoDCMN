from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import ReviewRequest
from .tasks import process_review_request_task
import logging

logger = logging.getLogger(__name__)


def _check_zoho_token(request):
    """Validate token from Zoho webhook."""
    token = request.headers.get('X-ZOHO-TOKEN') or request.META.get('HTTP_X_ZOHO_TOKEN')
    # fallback: allow token in JSON body
    if not token:
        try:
            token = request.data.get('token')
        except Exception:
            pass
    expected = getattr(settings, 'ZOHO_WEBHOOK_TOKEN', None)
    return bool(token and expected and token == expected)


class ReviewWebhookView(APIView):
    """
    Webhook endpoint for Zoho.
    Triggered when a lead reaches the "review" stage.
    
    Required fields:
        - email: Customer email address
    
    Optional fields:
        - name: Customer name
        - phone: Customer phone
        - contact_id: Zoho Contact ID (if not provided, will be fetched by email)
        - deal_id: Zoho Deal/Record ID
        - module: Zoho module name (Deals, Triple_Seal_Apostilles, etc.)
        - tracking_id: Tracking ID if exists
    
    Example payload:
    {
        "email": "customer@example.com",
        "name": "John Doe",
        "phone": "+1234567890",
        "deal_id": "5765XXXXXXXXXXXXXXX",
        "module": "Deals",
        "tracking_id": "FBI-ABC123"
    }
    """
    
    def post(self, request, format=None):
        if not _check_zoho_token(request):
            logger.warning("Review webhook: unauthorized request")
            return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        
        data = request.data
        logger.info(f"Review webhook received: {data}")
        
        # Required field - only email
        email = data.get('email')
        
        if not email:
            logger.warning(f"Review webhook missing email")
            return Response({
                'error': 'email is required',
                'received_data': data
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Optional fields
        name = data.get('name', '')
        phone = data.get('phone', '')
        contact_id = data.get('contact_id', '')  # Now optional
        deal_id = data.get('deal_id', '')
        module = data.get('module', '')
        tracking_id = data.get('tracking_id') or data.get('Tracking_ID', '')
        
        # Check if this deal_id was already processed (deduplication)
        if deal_id:
            existing = ReviewRequest.objects.filter(zoho_deal_id=deal_id).first()
            if existing:
                logger.info(f"Review request already exists for deal_id={deal_id}")
                return Response({
                    'ok': True,
                    'message': 'Review request already exists',
                    'review_request_id': existing.id
                }, status=status.HTTP_200_OK)
        
        # Create record (contact_id and leads_won will be fetched in task if needed)
        review_request = ReviewRequest.objects.create(
            email=email,
            name=name,
            phone=phone,
            zoho_contact_id=contact_id,
            zoho_deal_id=deal_id,
            zoho_module=module,
            tracking_id=tracking_id,
        )
        
        # Link Track if exists
        if tracking_id:
            from orders.models import Track
            track = Track.objects.filter(tid=tracking_id).first()
            if track:
                review_request.track = track
                review_request.save(update_fields=['track'])
        
        # Run async task for processing
        process_review_request_task.delay(review_request.id)
        
        logger.info(f"Review request created: id={review_request.id}, email={email}")
        
        return Response({
            'ok': True,
            'review_request_id': review_request.id,
            'message': 'Review request queued for processing'
        }, status=status.HTTP_201_CREATED)
