# orders/views/stripe.py
"""Stripe payment views and webhooks."""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
from django.core.mail import send_mail, EmailMessage
from django.template.loader import render_to_string

from rest_framework.views import APIView
from rest_framework.response import Response

from ..models import (
    FbiApostilleOrder,
    MarriageOrder,
    FileAttachment,
    Track,
)
from ..utils import generate_tid
from ..constants import STAGE_DEFS
from ..tasks import sync_order_to_zoho_task, send_tracking_email_task
from ..services.files import build_file_links

import stripe
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class CreateStripeSessionView(APIView):
    """Create Stripe Checkout session for payment."""
    
    def post(self, request):
        order_id = request.data.get("order_id")
        order_type = request.data.get("order_type")
        
        logger.info(f"[Stripe Session] Received request: order_id={order_id}, order_type={order_type}")

        if not order_id or not order_type:
            logger.error(f"[Stripe Session] Missing parameters: order_id={order_id}, order_type={order_type}")
            return Response({"error": "order_id and order_type are required"}, status=400)

        # Get order and configure session params
        if order_type == "fbi":
            order = get_object_or_404(FbiApostilleOrder, id=order_id)
            product_name = f"FBI Apostille Order #{order.id}"
            unit_amount = int(order.total_price * 100)
            customer_email = order.email
            description = f"FBI Apostille Order #{order.id} — {order.package}"
        elif order_type == "marriage":
            order = get_object_or_404(MarriageOrder, id=order_id)
            product_name = f"Tripe Seal Marriage Certificate Deposit"
            unit_amount = int(order.total_price * 100)
            customer_email = order.email
            description = f"Marriage Certificate Order #{order.id}"
        else:
            return Response({"error": "Invalid order_type"}, status=400)

        try:
            # Create TID now (sync to Zoho happens after payment)
            tid = generate_tid()
            codes = [d['code'] for d in STAGE_DEFS.get('fbi_apostille' if order_type == 'fbi' else 'marriage', [])]
            start_stage = codes[0] if codes else 'document_received'
            
            service_name = 'fbi_apostille' if order_type == 'fbi' else 'marriage'
            try:
                track = Track.objects.create(
                    tid=tid,
                    service=service_name,
                    data={
                        'name': order.name,
                        'email': order.email,
                        'service': service_name,
                        'current_stage': start_stage,
                        'order_id': order.id,
                        'order_type': order_type
                    }
                )
                # Link Track to Order
                order.track = track
                order.tid_created = True
                order.save(update_fields=['track', 'tid_created'])
                logger.info(f"[Stripe Session] Created TID={tid} for order {order_id}")
            except Exception as e:
                logger.exception(f"Failed to create Track for order {order_id}: {e}")
            
            success_url = f"{settings.FRONTEND_URL}/tracking?tid={tid}"

            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": product_name,
                        },
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url=success_url,
                cancel_url=settings.STRIPE_CANCEL_URL,
                metadata={
                    "order_id": str(order.id),
                    "order_type": order_type,
                    "tracking_id": tid,
                },
                customer_email=customer_email,
                payment_intent_data={
                    "description": description,
                }
            )

            return Response({"checkout_url": session.url})
        except Exception as e:
            return Response({"error": str(e)}, status=400)


@csrf_exempt
def stripe_webhook(request):
    """Handle Stripe webhook events (checkout.session.completed)."""
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        order_type = session.get("metadata", {}).get("order_type")
        tracking_id = session.get("metadata", {}).get("tracking_id")

        if not order_id or not order_type:
            return HttpResponse(status=400)

        try:
            if order_type == "fbi":
                _handle_fbi_payment(request, order_id, tracking_id)
            elif order_type == "marriage":
                _handle_marriage_payment(request, order_id, tracking_id)
            else:
                return HttpResponse(status=400)

        except Exception as e:
            logger.exception(f"[Webhook Error] {e}")
            return HttpResponse(status=400)

    return HttpResponse(status=200)


def _handle_fbi_payment(request, order_id, tracking_id):
    """Process FBI order after successful payment."""
    order = FbiApostilleOrder.objects.get(id=order_id)
    
    if not order.is_paid:
        order.is_paid = True
        order.save()

        # Pass tracking_id to Zoho sync
        sync_order_to_zoho_task.delay(order.id, "fbi", tracking_id=tracking_id)
        
        # Start tracking emails (Order Received)
        if tracking_id:
            try:
                send_tracking_email_task.delay(tracking_id, 'created')
            except Exception:
                logger.exception("Failed to queue tracking email for paid FBI order: %s", order.id)

    # Send email to manager (only if not sent before)
    if not order.manager_notified:
        file_links = build_file_links(request, order, html=False)

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        thread_id = f"<fbi-orders-thread-{today_str}@dcmobilenotary.com>"
        
        tid_info = f"Tracking ID: {tracking_id}\n" if tracking_id else ""
        
        email_body = (
            f"New FBI Apostille order has been paid! Order ID: {order.id}\n\n"
            f"{tid_info}"
            f"Name: {order.name}\n"
            f"Email: {order.email}\n"
            f"Phone: {order.phone}\n"
            f"Country: {order.country_name}\n"
            f"Address: {order.address}\n\n"
            f"Comments: \n{order.comments}\n\n"
            f"Package: {order.package.label}\n"
            f"Quantity: {order.count}\n"
            f"Shipping: {order.shipping_option.label}\n\n"
            f"Total: ${order.total_price}\n"
            f"Paid: ✅\n\n"
            f"Files:\n{file_links}"
        )
        try:
            email = EmailMessage(
                subject=f"✅ New Paid FBI Apostille Order — {today_str}",
                body=email_body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@dcmobilenotary.net"),
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            email.send()
            order.manager_notified = True
            order.save(update_fields=['manager_notified'])
            logger.info(f"✅ Manager notified for FBI order {order.id}")
        except Exception:
            logger.exception("Failed to send paid FBI order email for %s", order.id)

        # Client HTML email
        html_content = render_to_string("emails/fbi_order_paid.html", {
            "name": order.name,
            "order_id": order.id,
            "package": order.package.label,
            "count": order.count,
            "shipping": order.shipping_option.label,
            "total": order.total_price,
        })
        try:
            send_mail(
                subject="✅ Your Order Has Been Paid",
                message="Order Has Been Paid",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@dcmobilenotary.net"),
                recipient_list=[order.email],
                html_message=html_content,
                fail_silently=False,
            )
        except Exception as e:
            logger.error(f"[ERROR] Sending client email failed: {e}")


def _handle_marriage_payment(request, order_id, tracking_id):
    """Process Marriage order after successful payment."""
    order = MarriageOrder.objects.get(id=order_id)
    
    if not order.is_paid:
        order.is_paid = True
        sync_order_to_zoho_task.delay(order.id, "marriage")
        order.save()

    # Send email to manager (only if not sent before)
    if not order.manager_notified:
        ct = ContentType.objects.get_for_model(MarriageOrder)
        file_attachments = FileAttachment.objects.filter(
            content_type=ct,
            object_id=order.id
        )
        file_links = ""
        for att in file_attachments:
            file_links += f"📎 {request.build_absolute_uri(att.file.url)}\n"

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        thread_id = f"<marriage-orders-thread-{today_str}@dcmobilenotary.com>"
        email_body = (
            f"New Triple Seal Marriage Certificate order has been paid! Order ID: {order.id}\n\n"
            f"Name: {order.name}\n"
            f"Email: {order.email}\n"
            f"Phone: {order.phone}\n"
            f"Address: {order.address}\n\n"
            f"Husband: {order.husband_full_name}\n"
            f"Wife: {order.wife_full_name}\n"
            f"Marriage Date: {order.marriage_date}\n"
            f"Country: {order.country}\n"
            f"Certificate Number: {order.marriage_number}\n"
            f"------ OR ------\n\n"
            f"Files:\n{file_links if file_links else 'None'}\n\n"
            
            f"Comments: \n{order.comments}\n\n"
            
            f"Deposit: ${order.total_price}\n"
            f"Paid: ✅\n\n"
        )
        try:
            email = EmailMessage(
                subject=f"✅ New Paid Marriage Certificate Order — {today_str}",
                body=email_body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@dcmobilenotary.net"),
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<marriage-order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            email.send()
            order.manager_notified = True
            order.save(update_fields=['manager_notified'])
            logger.info(f"✅ Manager notified for Marriage order {order.id}")
        except Exception:
            logger.exception("Failed to send paid marriage order email for %s", order.id)

        # Client HTML email
        html_content = render_to_string("emails/marriage_order_paid.html", {
            "order_id": order.id,
            "name": order.name,
            "email": order.email,
            "phone": order.phone,
            "address": order.address,
            "total": order.total_price,
        })
        try:
            send_mail(
                subject="✅ Your Marriage Certificate Order Has Been Paid",
                message="Order Has Been Paid",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@dcmobilenotary.net"),
                recipient_list=[order.email],
                html_message=html_content,
                fail_silently=False,
            )
        except Exception as e:
            logger.exception(f"Failed to send client marriage email for order {order.id}: {e}")
