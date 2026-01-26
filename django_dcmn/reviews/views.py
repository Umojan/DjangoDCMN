from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import ReviewRequest
from .tasks import process_review_request_task
import logging

logger = logging.getLogger(__name__)


def _check_zoho_token(request):
    """Проверка токена от Zoho webhook."""
    token = request.headers.get('X-Zoho-Token') or request.data.get('token')
    expected = getattr(settings, 'ZOHO_WEBHOOK_TOKEN', None)
    return bool(token and expected and token == expected)


class ReviewWebhookView(APIView):
    """
    Webhook endpoint для Zoho.
    Вызывается когда лид достигает стадии "review".
    
    Required fields:
        - email: Customer email address
        - contact_id: Zoho Contact ID
    
    Optional fields:
        - name: Customer name
        - phone: Customer phone
        - deal_id: Zoho Deal/Record ID
        - module: Zoho module name (Deals, Triple_Seal_Apostilles, etc.)
        - tracking_id: Tracking ID if exists
    
    Example payload:
    {
        "email": "customer@example.com",
        "name": "John Doe",
        "phone": "+1234567890",
        "contact_id": "5765XXXXXXXXXXXXXXX",
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
        
        # Required fields
        email = data.get('email')
        contact_id = data.get('contact_id')
        
        if not email or not contact_id:
            return Response({
                'error': 'email and contact_id are required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Optional fields
        name = data.get('name', '')
        phone = data.get('phone', '')
        deal_id = data.get('deal_id', '')
        module = data.get('module', '')
        tracking_id = data.get('tracking_id') or data.get('Tracking_ID', '')
        
        # Проверяем, не обрабатывали ли уже этот deal_id (дедупликация)
        if deal_id:
            existing = ReviewRequest.objects.filter(zoho_deal_id=deal_id).first()
            if existing:
                logger.info(f"Review request already exists for deal_id={deal_id}")
                return Response({
                    'ok': True,
                    'message': 'Review request already exists',
                    'review_request_id': existing.id
                }, status=status.HTTP_200_OK)
        
        # Создаем запись
        review_request = ReviewRequest.objects.create(
            email=email,
            name=name,
            phone=phone,
            zoho_contact_id=contact_id,
            zoho_deal_id=deal_id,
            zoho_module=module,
            tracking_id=tracking_id,
        )
        
        # Привязываем Track если есть
        if tracking_id:
            from orders.models import Track
            track = Track.objects.filter(tid=tracking_id).first()
            if track:
                review_request.track = track
                review_request.save(update_fields=['track'])
        
        # Запускаем async task для обработки
        process_review_request_task.delay(review_request.id)
        
        logger.info(f"Review request created: id={review_request.id}, email={email}")
        
        return Response({
            'ok': True,
            'review_request_id': review_request.id,
            'message': 'Review request queued for processing'
        }, status=status.HTTP_201_CREATED)
