import logging

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from orders.utils import check_zoho_webhook_token
from .models import Review, ReviewRequest
from .services import (
    parse_token,
    positive_threshold,
    public_context,
    record_rating,
    redirect_target,
    service_label,
)
from .tasks import (
    notify_negative_task,
    process_review_request_task,
    send_trustpilot_invite_task,
)

logger = logging.getLogger(__name__)

NEGATIVE_ALERT_DELAY_SECONDS = 15 * 60  # give the customer time to write feedback before alerting


def _client_ip(request) -> str | None:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:45]
    return request.META.get('REMOTE_ADDR')


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
        if not check_zoho_webhook_token(request):
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


@method_decorator(csrf_exempt, name='dispatch')
class RateView(View):
    """Star link target from the review email.

    GET  → tiny interstitial page that auto-submits a POST (so link scanners / email
           security bots that prefetch URLs never record a rating).
    POST → records the rating (first click wins) and redirects:
           positive → Google / Trustpilot thank-you page, negative → internal feedback form.
    """

    def get(self, request, token: str, stars: int):
        rr = parse_token(token)
        if rr is None:
            return render(request, 'reviews/rate_invalid.html', status=404)
        stars = max(1, min(5, int(stars)))
        return render(request, 'reviews/rate_redirect.html', {
            'stars': stars,
            'name': rr.first_name,
            'service_label': service_label(rr.zoho_module),
            'positive': stars >= positive_threshold(),
        })

    def post(self, request, token: str, stars: int):
        rr = parse_token(token)
        if rr is None:
            return render(request, 'reviews/rate_invalid.html', status=404)

        review, created = record_rating(
            rr, int(stars), ip=_client_ip(request), user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        if created:
            logger.info('Review %s rated %s★ → %s', rr.id, review.rating, review.route)
            if review.route == Review.ROUTE_TRUSTPILOT:
                send_trustpilot_invite_task.delay(rr.id)
            elif review.route == Review.ROUTE_INTERNAL:
                notify_negative_task.apply_async((review.id,), countdown=NEGATIVE_ALERT_DELAY_SECONDS)
        return HttpResponseRedirect(redirect_target(review, token))


class ReviewContextView(APIView):
    """Public context for /feedback and /review-thanks pages: who the customer is, what order."""

    def get(self, request, token: str):
        rr = parse_token(token)
        if rr is None:
            return Response({'detail': 'This link is invalid or has expired.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(public_context(rr))


class FeedbackView(APIView):
    """Internal (negative) feedback form submit from /feedback."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'reviews_feedback'

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        token = str(data.get('token') or data.get('t') or '').strip()
        rr = parse_token(token) if token else None
        if rr is None:
            return Response({'detail': 'This link is invalid or has expired.'}, status=status.HTTP_400_BAD_REQUEST)

        message = str(data.get('message') or data.get('feedback') or '').strip()
        callback = str(data.get('callback_requested') or data.get('callback') or '').lower() in ('1', 'true', 'on', 'yes')
        phone = str(data.get('phone') or '').strip()[:50]
        rating_raw = data.get('rating')

        if not message and not callback:
            return Response({'detail': 'Please tell us what went wrong, or ask us to call you back.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(message) > 5000:
            message = message[:5000]

        review = getattr(rr, 'review', None)
        if review is None:
            # Customer landed on the form without clicking a star (e.g. shared link) — treat as negative.
            try:
                rating = max(1, min(5, int(rating_raw)))
            except (TypeError, ValueError):
                rating = None
            review = Review(request=rr, rating=rating, route=Review.ROUTE_INTERNAL, rated_at=timezone.now())
        if review.feedback_at and review.feedback_text and review.feedback_text != message:
            # Second submission: append rather than overwrite.
            message = f"{review.feedback_text}\n\n--- update {timezone.now():%Y-%m-%d %H:%M} ---\n{message}"
        review.feedback_text = message
        review.callback_requested = callback
        review.callback_phone = phone
        review.feedback_at = timezone.now()
        if not review.ip:
            review.ip = _client_ip(request)
            review.user_agent = request.META.get('HTTP_USER_AGENT', '')[:300]
        review.save()

        notify_negative_task.delay(review.id)
        return Response({'ok': True, 'id': review.id})
