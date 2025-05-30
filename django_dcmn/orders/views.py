# orders/views.py
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.core.mail import send_mail, EmailMessage
from django.template.loader import render_to_string

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import FbiApostilleOrderSerializer, OrderFileSerializer
from .models import FbiApostilleOrder, FbiServicePackage, FbiPricingSettings, ShippingOption, OrderFile

import stripe


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
                    for f in request.FILES.getlist('files'):
                        file_instance = OrderFile.objects.create(order=order, file=f)
                        file_urls.append(request.build_absolute_uri(file_instance.file.url))

                return Response({
                    'message': 'Order created',
                    'order_id': order.id,
                    'file_urls': file_urls or None,
                    'calculated_total': float(total),
                }, status=status.HTTP_201_CREATED)

            except (FbiServicePackage.DoesNotExist, ShippingOption.DoesNotExist):
                return Response({'error': 'Invalid package or shipping option.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# Get form options
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


stripe.api_key = settings.STRIPE_SECRET_KEY


class CreateStripeSessionView(APIView):
    def post(self, request):
        order_id = request.data.get("order_id")
        order = get_object_or_404(FbiApostilleOrder, id=order_id)

        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"FBI Apostille Order #{order.id}",
                        },
                        "unit_amount": int(order.total_price * 100),  # in cents
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url=f"{settings.STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=settings.STRIPE_CANCEL_URL,
                metadata={
                    "order_id": str(order.id),
                },
                customer_email=order.email,
                payment_intent_data={
                    "description": f"FBI Apostille Order #{order.id} — {order.package}",
                }
            )

            return Response({"checkout_url": session.url})

        except Exception as e:
            return Response({"error": str(e)}, status=400)


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")

        if order_id:
            try:
                order = FbiApostilleOrder.objects.get(id=order_id)
                if not order.is_paid:
                    order.is_paid = True
                    order.save()

                    file_links = ""
                    for f in order.files.all():
                        file_links += f"📎 {request.build_absolute_uri(f.file.url)}\n"

                    # Send email to office
                    email_body = (
                        f"New FBI Apostille order has been paid!\n\n"
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

                    from datetime import datetime
                    today_str = datetime.utcnow().strftime("%Y-%m-%d")
                    thread_id = f"<fbi-orders-thread-{today_str}@dcmobilenotary.com>"
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

                    file_links_html = "".join([
                        f'<li><a href="{request.build_absolute_uri(f.file.url)}">{f.file.name}</a></li>'
                        for f in order.files.all()
                    ]) or "<li>No files attached</li>"

                    html_content = render_to_string("emails/fbi_order_paid.html", {
                        "name": order.name,
                        "order_id": order.id,
                        "package": order.package.label,
                        "count": order.count,
                        "shipping": order.shipping_option.label,
                        "total": order.total_price,
                        "files": file_links_html
                    })

                    # Send email to the client
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

            except FbiApostilleOrder.DoesNotExist:
                pass

    return HttpResponse(status=200)


def test_email(request):
    send_mail(
        subject="🚀 Django Email Test",
        message="If you're reading this, your email setup works perfectly!",
        from_email="support@dcmobilenotary.net",
        recipient_list=["support@dcmobilenotary.com"],
        fail_silently=False,
    )
    return JsonResponse({"status": "✅ Email sent!"})
