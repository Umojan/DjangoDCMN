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
    token = request.headers.get('X-Zoho-Token') or request.data.get('token')
    expected = getattr(settings, 'ZOHO_WEBHOOK_TOKEN', None)
    return bool(token and expected and token == expected)


class ReviewWebhookView(APIView):
    """
    Webhook endpoint for Zoho.
    Triggered when a lead reaches the "review" stage.
    
    Required fields:
        - email: Customer email address
        - contact_id: Zoho Contact ID
    
    Optional fields:
        - name: Customer name
        - phone: Customer phone
        - contact_won_leads: Number of Leads Won from Zoho Contact
        - deal_id: Zoho Deal/Record ID
        - module: Zoho module name (Deals, Triple_Seal_Apostilles, etc.)
        - tracking_id: Tracking ID if exists
    
    Example payload:
    {
        "email": "customer@example.com",
        "name": "John Doe",
        "phone": "+1234567890",
        "contact_id": "5765XXXXXXXXXXXXXXX",
        "contact_won_leads": "0",
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
        logger.info(f"Review webhook - email: '{data.get('email')}', contact_id: '{data.get('contact_id')}'")
        
        # Required fields
        email = data.get('email')
        contact_id = data.get('contact_id')
        
        if not email or not contact_id:
            logger.warning(f"Review webhook missing fields - email: '{email}', contact_id: '{contact_id}'")
            return Response({
                'error': 'email and contact_id are required',
                'received_email': email,
                'received_contact_id': contact_id
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Optional fields
        name = data.get('name', '')
        phone = data.get('phone', '')
        deal_id = data.get('deal_id', '')
        module = data.get('module', '')
        tracking_id = data.get('tracking_id') or data.get('Tracking_ID', '')
        
        # Leads Won from Zoho Contact (passed directly from webhook)
        contact_won_leads = data.get('contact_won_leads')
        leads_won = 0
        if contact_won_leads is not None and contact_won_leads != '':
            try:
                leads_won = int(contact_won_leads)
            except (ValueError, TypeError):
                leads_won = 0
        
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
        
        # Create record
        review_request = ReviewRequest.objects.create(
            email=email,
            name=name,
            phone=phone,
            zoho_contact_id=contact_id,
            zoho_deal_id=deal_id,
            zoho_module=module,
            tracking_id=tracking_id,
            leads_won_before=leads_won,
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
