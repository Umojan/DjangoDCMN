# orders/views.py
from django.conf import settings
from django.shortcuts import get_object_or_404

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
                }
            )

            return Response({"checkout_url": session.url})

        except Exception as e:
            return Response({"error": str(e)}, status=400)