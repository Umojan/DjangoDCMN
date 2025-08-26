# orders/views.py
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.core.mail import send_mail, EmailMessage
from django.template.loader import render_to_string

from .tasks import sync_order_to_zoho_task

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import (
    FbiApostilleOrderSerializer,
    MarriageOrderSerializer,
    EmbassyLegalizationOrderSerializer,
    ApostilleOrderSerializer,
    I9OrderSerializer,
    TranslationOrderSerializer,
    QuoteRequestSerializer
)
from .models import (
    FbiApostilleOrder,
    FbiServicePackage,
    FbiPricingSettings,
    ShippingOption,
    MarriageOrder,
    MarriagePricingSettings,
    FileAttachment,
    EmbassyLegalizationOrder,
    TranslationOrder,
    I9VerificationOrder
)

import stripe
import logging

from datetime import datetime


class CreateFbiOrderView(APIView):
    def post(self, request, format=None):
        serializer = FbiApostilleOrderSerializer(data=request.data)
        if serializer.is_valid():
            try:
                package = FbiServicePackage.objects.get(code=request.data['package'])
                shipping = ShippingOption.objects.get(code=request.data['shipping_option'])
                count = int(request.data['count'])

                # Получаем цену за 1 certificate из настроек
                price_setting = FbiPricingSettings.objects.first()
                per_certificate_price = price_setting.price_per_certificate if price_setting else 25

                total = package.price + shipping.price + (count * per_certificate_price)

                order = serializer.save(
                    total_price=total,
                    package=package,
                    shipping_option=shipping
                )

                file_urls = []
                if request.FILES:
                    ct = ContentType.objects.get_for_model(FbiApostilleOrder)
                    for f in request.FILES.getlist('files'):
                        attachment = FileAttachment.objects.create(
                            content_type=ct,
                            object_id=order.id,
                            file=f
                        )
                        file_urls.append(request.build_absolute_uri(attachment.file.url))

                return Response({
                    'message': 'Order created',
                    'order_id': order.id,
                    'file_urls': file_urls or None,
                    'calculated_total': float(total),
                }, status=status.HTTP_201_CREATED)

            except (FbiServicePackage.DoesNotExist, ShippingOption.DoesNotExist):
                return Response({'error': 'Invalid package or shipping option.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FbiOptionsView(APIView):
    def get(self, request, format=None):
        packages = FbiServicePackage.objects.values('id', 'code', 'label', 'price')
        shipping = ShippingOption.objects.values('id', 'code', 'label', 'price')
        price_setting = FbiPricingSettings.objects.first()
        price_per_certificate = price_setting.price_per_certificate if price_setting else 25.00

        return Response({
            'packages': list(packages),
            'shipping_options': list(shipping),
            'price_per_certificate': price_per_certificate
        })


class CreateMarriageOrderView(APIView):
    def post(self, request, format=None):
        serializer = MarriageOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        marriage_order = MarriageOrder.objects.create(**serializer.validated_data)

        price_setting = MarriagePricingSettings.objects.first()
        base_price = price_setting.price if price_setting else 0.00

        marriage_order.total_price = base_price
        marriage_order.save()

        file_urls = []
        if request.FILES:
            ct = ContentType.objects.get_for_model(MarriageOrder)
            for f in request.FILES.getlist('files'):
                attachment = FileAttachment.objects.create(
                    content_type=ct,
                    object_id=marriage_order.id,
                    file=f
                )
                file_urls.append(request.build_absolute_uri(attachment.file.url))

        return Response({
            'message': 'Marriage order created',
            'order_id': marriage_order.id,
            'calculated_total': float(marriage_order.total_price),
            'file_urls': file_urls or None
        }, status=status.HTTP_201_CREATED)


class CreateEmbassyOrderView(APIView):
    def post(self, request, format=None):
        serializer = EmbassyLegalizationOrderSerializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()
            try:
                sync_order_to_zoho_task.delay(order.id, "embassy")
            except Exception:
                logging.exception("Failed to enqueue Zoho sync task for embassy order %s", order.id)

            file_urls = []
            if request.FILES:
                ct = ContentType.objects.get_for_model(EmbassyLegalizationOrder)
                for f in request.FILES.getlist('files'):
                    attachment = FileAttachment.objects.create(
                        content_type=ct,
                        object_id=order.id,
                        file=f
                    )
                    file_urls.append(request.build_absolute_uri(attachment.file.url))

            # Send email to staff
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            thread_id = f"<embassy-orders-thread-{today_str}@dcmobilenotary.com>"

            file_links = ""
            for f in order.file_attachments.all():
                file_links += f"📎 {request.build_absolute_uri(f.file.url)}\n"

            email_body = (
                f"New Embassy Legalization order submitted! Order ID: {order.id}\n\n"
                f"Name: {order.name}\n"
                f"Email: {order.email}\n"
                f"Phone: {order.phone}\n"
                f"Address: {order.address}\n"
                f"Document Type: {order.document_type}\n"
                f"Country: {order.country}\n"
                f"Comments: {order.comments}\n\n"
                f"Files:\n{file_links or 'None'}"
            )

            email = EmailMessage(
                subject=f"📄 New Embassy Legalization Order — {today_str}",
                body=email_body,
                from_email=settings.EMAIL_HOST_USER,
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<embassy-order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            try:
                email.send()
            except Exception:
                logging.exception("Failed to send embassy order email for %s", order.id)

            return Response({
                'message': 'Embassy legalization order created',
                'order_id': order.id,
                'file_urls': file_urls or None
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateApostilleOrderView(APIView):
    def post(self, request, format=None):
        serializer = ApostilleOrderSerializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()
            try:
                sync_order_to_zoho_task.delay(order.id, "apostille")
            except Exception:
                logging.exception("Failed to enqueue Zoho sync task for apostille order %s", order.id)

            # Send email to staff
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            thread_id = f"<apostille-orders-thread-{today_str}@dcmobilenotary.com>"

            email_body = (
                f"New Apostille order submitted! Order ID: {order.id}\n\n"
                f"Name: {order.name}\n"
                f"Email: {order.email}\n"
                f"Phone: {order.phone}\n"
                f"Documents Type: {order.type}\n"
                f"Country: {order.country}\n"
                f"Service Type: {order.service_type}\n\n"
            )

            if order.service_type == "My Address" and order.address:
                email_body += f"Address: {order.address}\n\n"

            if order.comments:
                email_body += f"Comments: {order.comments}"

            email = EmailMessage(
                subject=f"📄 New Apostille Order — {today_str}",
                body=email_body,
                from_email=settings.EMAIL_HOST_USER,
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<apostille-order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            try:
                email.send()
            except Exception:
                logging.exception("Failed to send apostille order email for %s", order.id)

            return Response({
                'message': 'Apostille order created',
                'order_id': order.id,
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateTranslationOrderView(APIView):
    def post(self, request, format=None):
        serializer = TranslationOrderSerializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()
            try:
                sync_order_to_zoho_task.delay(order.id, "translation")
            except Exception:
                logging.exception("Failed to enqueue Zoho sync task for translation order %s", order.id)

            # Save uploaded files
            file_urls = []
            if request.FILES:
                ct = ContentType.objects.get_for_model(TranslationOrder)
                for f in request.FILES.getlist('files'):
                    attachment = FileAttachment.objects.create(
                        content_type=ct,
                        object_id=order.id,
                        file=f
                    )
                    file_urls.append(request.build_absolute_uri(attachment.file.url))

            # Send email to staff
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            thread_id = f"<translation-orders-thread-{today_str}@dcmobilenotary.com>"

            file_links = ""
            for f in order.file_attachments.all():
                file_links += f"📎 {request.build_absolute_uri(f.file.url)}\n"

            email_body = (
                f"New Translation Order submitted! Order ID: {order.id}\n\n"
                f"Name: {order.name}\n"
                f"Email: {order.email}\n"
                f"Phone: {order.phone}\n"
                f"Address: {order.address}\n"
                f"Languages: {order.languages}\n"
                f"Comments: \n{order.comments}\n\n"
                f"Files:\n{file_links if file_links else 'None'}"
            )

            email = EmailMessage(
                subject=f"📄 New Translation Order — {today_str}",
                body=email_body,
                from_email=settings.EMAIL_HOST_USER,
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<translation-order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            try:
                email.send()
            except Exception:
                logging.exception("Failed to send translation order email for %s", order.id)

            return Response({
                'message': 'Translation order created',
                'order_id': order.id,
                'file_urls': file_urls or None
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateQuoteRequestView(APIView):
    def post(self, request, format=None):
        serializer = QuoteRequestSerializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()
            try:
                sync_order_to_zoho_task.delay(order.id, "quote")
            except Exception:
                logging.exception("Failed to enqueue Zoho sync task for quote request %s", order.id)


            # Send email to staff
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            thread_id = f"<quote-request-thread-{today_str}@dcmobilenotary.com>"

            email_body = (
                f"New Quote Request submitted! Request ID: {order.id}\n\n"
                f"Name: {order.name}\n"
                f"Email: {order.email}\n"
                f"Phone: {order.phone}\n"
                f"Date: {order.appointment_date}, Time: {order.appointment_time}\n"
                f"Address: {order.address}\n"
                f"Number of documents: {order.number}\n"
                f"Services: {order.services}\n\n"
                f"Message: \n{order.comments}\n"
            )

            email = EmailMessage(
                subject=f"❓ New Quote Request — {today_str}",
                body=email_body,
                from_email=settings.EMAIL_HOST_USER,
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<quote-request-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            try:
                email.send()
            except Exception:
                logging.exception("Failed to send quote request email for %s", order.id)

            return Response({
                'message': 'Quote request created',
                'order_id': order.id,
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateI9OrderView(APIView):
    def post(self, request, format=None):
        serializer = I9OrderSerializer(data=request.data)
        if serializer.is_valid():
            order = serializer.save()
            try:
                sync_order_to_zoho_task.delay(order.id, "I-9")
            except Exception:
                logging.exception("Failed to enqueue Zoho sync task for I-9 order %s", order.id)

            file_urls = []
            if request.FILES:
                ct = ContentType.objects.get_for_model(I9VerificationOrder)
                for f in request.FILES.getlist('files'):
                    attachment = FileAttachment.objects.create(
                        content_type=ct,
                        object_id=order.id,
                        file=f
                    )
                    file_urls.append(request.build_absolute_uri(attachment.file.url))

            # Email to staff
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            thread_id = f"<i9-orders-thread-{today_str}@dcmobilenotary.com>"

            file_links = ""
            for f in order.file_attachments.all():
                file_links += f"📎 {request.build_absolute_uri(f.file.url)}\n"

            email_body = (
                f"New I-9 Verification Order submitted! Order ID: {order.id}\n\n"
                f"Name: {order.name}\n"
                f"Email: {order.email}\n"
                f"Phone: {order.phone}\n"
                f"Address: {order.address}\n"
                f"Date: {order.appointment_date}\n"
                f"Time: {order.appointment_time}\n\n"
                f"Comments: \n{order.comments or 'None'}\n\n"

                f"Files:\n{file_links if file_links else 'None'}"
            )

            email = EmailMessage(
                subject=f"📄 New I-9 Verification Order — {today_str}",
                body=email_body,
                from_email=settings.EMAIL_HOST_USER,
                to=settings.EMAIL_OFFICE_RECEIVER,
                headers={
                    "Message-ID": f"<i9-order-{order.id}@dcmobilenotary.com>",
                    "In-Reply-To": thread_id,
                    "References": thread_id,
                }
            )
            try:
                email.send()
            except Exception:
                logging.exception("Failed to send I-9 order email for %s", order.id)

            return Response({
                'message': 'I-9 Verification order created',
                'order_id': order.id
            }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


stripe.api_key = settings.STRIPE_SECRET_KEY


class CreateStripeSessionView(APIView):
    def post(self, request):
        order_id = request.data.get("order_id")
        order_type = request.data.get("order_type")

        # Определяем модель и параметры для заказа
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
                success_url=f"{settings.STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=settings.STRIPE_CANCEL_URL,
                metadata={
                    "order_id": str(order.id),
                    "order_type": order_type,
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
    from datetime import datetime
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

        if not order_id or not order_type:
            return HttpResponse(status=400)

        try:
            # === FBI Apostille Order ===
            if order_type == "fbi":
                from .models import FbiApostilleOrder
                order = FbiApostilleOrder.objects.get(id=order_id)
                if not order.is_paid:
                    order.is_paid = True
                    sync_order_to_zoho_task.delay(order.id, "fbi")
                    order.save()

                    # Files (universal)
                    file_links = ""
                    for f in order.file_attachments.all():
                        file_links += f"📎 {request.build_absolute_uri(f.file.url)}\n"

                    today_str = datetime.utcnow().strftime("%Y-%m-%d")
                    thread_id = f"<fbi-orders-thread-{today_str}@dcmobilenotary.com>"
                    email_body = (
                        f"New FBI Apostille order has been paid! Order ID: {order.id}\n\n"
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
                        f"Files:\n{file_links if file_links else 'None'}"
                    )
                    email = EmailMessage(
                        subject=f"✅ New Paid FBI Apostille Order — {today_str}",
                        body=email_body,
                        from_email=settings.EMAIL_HOST_USER,
                        to=settings.EMAIL_OFFICE_RECEIVER,
                        headers={
                            "Message-ID": f"<order-{order.id}@dcmobilenotary.com>",
                            "In-Reply-To": thread_id,
                            "References": thread_id,
                        }
                    )
                    email.send()

                    # Клиенту HTML-письмо (пример)
                    file_links_html = "".join([
                        f'<li><a href="{request.build_absolute_uri(f.file.url)}">{f.file.name}</a></li>'
                        for f in order.file_attachments.all()
                    ]) or "<li>No files attached</li>"
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
                            from_email=settings.EMAIL_HOST_USER,
                            recipient_list=[order.email],
                            html_message=html_content,
                            fail_silently=False,
                        )
                    except Exception as e:
                        print("[ERROR] Sending client email failed:", e)

            # === Marriage Order ===
            elif order_type == "marriage":
                from .models import MarriageOrder, FileAttachment
                order = MarriageOrder.objects.get(id=order_id)
                if not order.is_paid:
                    order.is_paid = True
                    sync_order_to_zoho_task.delay(order.id, "marriage")
                    order.save()

                    # Файлы (через универсальные FileAttachment)
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
                    email = EmailMessage(
                        subject=f"✅ New Paid Marriage Certificate Order — {today_str}",
                        body=email_body,
                        from_email=settings.EMAIL_HOST_USER,
                        to=settings.EMAIL_OFFICE_RECEIVER,
                        headers={
                            "Message-ID": f"<marriage-order-{order.id}@dcmobilenotary.com>",
                            "In-Reply-To": thread_id,
                            "References": thread_id,
                        }
                    )
                    email.send()

                    # Клиенту HTML-письмо (аналогично, если есть темплейт)
                    file_links_html = "".join([
                        f'<li><a href="{request.build_absolute_uri(att.file.url)}">{att.file.name}</a></li>'
                        for att in file_attachments
                    ]) or "<li>No files attached</li>"
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
                            from_email=settings.EMAIL_HOST_USER,
                            recipient_list=[order.email],
                            html_message=html_content,
                            fail_silently=False,
                        )
                    except Exception as e:
                        print("[ERROR] Sending client marriage email failed:", e)

            else:
                return HttpResponse(status=400)

        except Exception as e:
            print(f"[Webhook Error] {e}")
            return HttpResponse(status=400)

    return HttpResponse(status=200)


def test_email(request):
    try:
        send_mail(
            subject="🚀 Django Email Test via Resend",
            message="If you see this, Resend is working perfectly!",
            from_email="support@dcmobilenotary.net",
            recipient_list=["support@dcmobilenotary.com"],
            fail_silently=False,
        )
        return JsonResponse({"status": "✅ Email sent!"})
    except Exception as e:
        return JsonResponse({"status": "❌ Failed", "error": str(e)}, status=500)


def zoho_callback(request):
    code = request.GET.get('code')
    if code:
        return HttpResponse(f'Authorization code: {code}')
    return HttpResponse('No code found', status=400)
