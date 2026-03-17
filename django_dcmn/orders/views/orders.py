# orders/views/orders.py
"""Order creation views."""

from django.contrib.contenttypes.models import ContentType
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from ..serializers import (
    FbiApostilleOrderSerializer,
    MarriageOrderSerializer,
    EmbassyLegalizationOrderSerializer,
    ApostilleOrderSerializer,
    TranslationOrderSerializer,
    QuoteRequestSerializer,
    I9OrderSerializer,
    PreCheckSubmissionSerializer,
    FingerprintingSubmissionSerializer,
)
from ..models import (
    FbiApostilleOrder,
    FbiServicePackage,
    FbiPricingSettings,
    ShippingOption,
    MarriageOrder,
    MarriagePricingSettings,
    EmbassyLegalizationOrder,
    TranslationOrder,
    ApostilleOrder,
    I9VerificationOrder,
    PreCheckSubmission,
    FingerprintingSubmission,
    FileAttachment,
)
from ..services import process_new_order, save_file_attachments
from ..services.attribution import process_attribution
from ..tasks import sync_order_to_zoho_task

import logging

logger = logging.getLogger(__name__)


class CreateFbiOrderView(APIView):
    """
    Create FBI Apostille order.
    TID, Zoho sync, and emails are handled AFTER payment in stripe webhook.
    """
    
    def post(self, request, format=None):
        serializer = FbiApostilleOrderSerializer(data=request.data)
        if serializer.is_valid():
            try:
                package = FbiServicePackage.objects.get(code=request.data['package'])
                shipping = ShippingOption.objects.get(code=request.data['shipping_option'])
                count = int(request.data['count'])

                price_setting = FbiPricingSettings.objects.first()
                per_certificate_price = price_setting.price_per_certificate if price_setting else 25

                total = package.price + shipping.price + (count * per_certificate_price)

                order = serializer.save(
                    total_price=total,
                    package=package,
                    shipping_option=shipping
                )

                # Process attribution data
                process_attribution(request, order)

                file_urls = save_file_attachments(request, FbiApostilleOrder, order)

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
    """Get FBI Apostille options (packages, shipping, pricing)."""
    
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
    """
    Create Marriage Certificate order.
    TID, Zoho sync, and emails are handled AFTER payment in stripe webhook.
    """
    
    def post(self, request, format=None):
        serializer = MarriageOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        marriage_order = MarriageOrder.objects.create(**serializer.validated_data)

        price_setting = MarriagePricingSettings.objects.first()
        base_price = price_setting.price if price_setting else 0.00

        marriage_order.total_price = base_price
        marriage_order.save()

        # Process attribution data
        process_attribution(request, marriage_order)

        file_urls = save_file_attachments(request, MarriageOrder, marriage_order)

        return Response({
            'message': 'Marriage order created',
            'order_id': marriage_order.id,
            'calculated_total': float(marriage_order.total_price),
            'file_urls': file_urls or None
        }, status=status.HTTP_201_CREATED)


class CreateEmbassyOrderView(APIView):
    """Create Embassy Legalization order with full processing pipeline."""
    
    def post(self, request, format=None):
        serializer = EmbassyLegalizationOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        order = serializer.save()
        
        result = process_new_order(
            request=request,
            order=order,
            model_class=EmbassyLegalizationOrder,
            order_type='embassy',
            sync_to_zoho=True,
            create_tracking=True,
            send_notification=True,
            send_welcome_email=True,
        )

        return Response({
            'message': 'Embassy legalization order created',
            'order_id': result['order_id'],
            'tracking_id': result['tracking_id'],
            'file_urls': result['file_urls']
        }, status=status.HTTP_201_CREATED)


class CreateApostilleOrderView(APIView):
    """Create Apostille order with full processing pipeline."""

    def post(self, request, format=None):
        try:
            logger.info(f"[Apostille] Received request data: {request.data}")

            serializer = ApostilleOrderSerializer(data=request.data)
            if not serializer.is_valid():
                logger.warning(f"[Apostille] Validation errors: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

            logger.info(f"[Apostille] Serializer valid, saving order...")
            order = serializer.save()
            logger.info(f"[Apostille] Order saved: {order.id}")

            logger.info(f"[Apostille] Starting process_new_order...")
            result = process_new_order(
                request=request,
                order=order,
                model_class=ApostilleOrder,
                order_type='apostille',
                sync_to_zoho=True,
                create_tracking=True,
                send_notification=True,
                send_welcome_email=True,
            )
            logger.info(f"[Apostille] process_new_order completed: {result}")

            return Response({
                'message': 'Apostille order created',
                'order_id': result['order_id'],
                'tracking_id': result['tracking_id'],
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception(f"[Apostille] ❌ UNEXPECTED ERROR: {e}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateTranslationOrderView(APIView):
    """Create Translation order with full processing pipeline."""
    
    def post(self, request, format=None):
        serializer = TranslationOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        order = serializer.save()
        
        result = process_new_order(
            request=request,
            order=order,
            model_class=TranslationOrder,
            order_type='translation',
            sync_to_zoho=True,
            create_tracking=True,
            send_notification=True,
            send_welcome_email=True,
        )

        return Response({
            'message': 'Translation order created',
            'order_id': result['order_id'],
            'tracking_id': result['tracking_id'],
            'file_urls': result['file_urls']
        }, status=status.HTTP_201_CREATED)


class CreateQuoteRequestView(APIView):
    """Create Quote Request with Zoho sync and staff notification."""
    
    def post(self, request, format=None):
        serializer = QuoteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        order = serializer.save()

        # Process attribution data
        process_attribution(request, order)

        # Quote requests don't have file attachments or tracking
        from ..services.notifications import send_staff_notification, build_order_extra_body

        # Sync to Zoho
        try:
            sync_order_to_zoho_task.delay(order.id, "quote")
        except Exception:
            logger.exception("Failed to enqueue Zoho sync task for quote request %s", order.id)
        
        # Send staff notification
        extra_body = build_order_extra_body(order, 'quote')
        send_staff_notification(
            order=order,
            order_type='quote',
            extra_body=extra_body,
        )

        return Response({
            'message': 'Quote request created',
            'order_id': order.id,
        }, status=status.HTTP_201_CREATED)


class CreateI9OrderView(APIView):
    """Create I-9 Verification order with Zoho sync and staff notification."""
    
    def post(self, request, format=None):
        serializer = I9OrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        order = serializer.save()

        # Process attribution data
        process_attribution(request, order)

        # Sync to Zoho
        try:
            sync_order_to_zoho_task.delay(order.id, "I-9")
        except Exception:
            logger.exception("Failed to enqueue Zoho sync task for I-9 order %s", order.id)
        
        # Save files
        file_urls = save_file_attachments(request, I9VerificationOrder, order)
        
        # Send staff notification
        from ..services.notifications import send_staff_notification, build_order_extra_body
        from ..services.files import build_file_links
        
        extra_body = build_order_extra_body(order, 'i9')
        file_links = build_file_links(request, order, html=False)
        send_staff_notification(
            order=order,
            order_type='i9',
            extra_body=extra_body,
            file_links=file_links,
        )

        return Response({
            'message': 'I-9 Verification order created',
            'order_id': order.id
        }, status=status.HTTP_201_CREATED)


class CreatePreCheckView(APIView):
    """Create Pre-Check Document Review submission with Zoho sync and staff notification."""

    def post(self, request, format=None):
        serializer = PreCheckSubmissionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        order = serializer.save()

        # Process attribution data
        process_attribution(request, order)

        # Save file attachments
        file_urls = save_file_attachments(request, PreCheckSubmission, order)

        # Sync to Zoho
        try:
            sync_order_to_zoho_task.delay(order.id, "pre-check")
        except Exception:
            logger.exception("Failed to enqueue Zoho sync task for pre-check %s", order.id)

        # Send staff notification
        from ..services.notifications import send_staff_notification, build_order_extra_body
        from ..services.files import build_file_links

        extra_body = build_order_extra_body(order, 'pre-check')
        file_links = build_file_links(request, order, html=False)
        send_staff_notification(
            order=order,
            order_type='pre-check',
            extra_body=extra_body,
            file_links=file_links,
        )

        return Response({
            'message': 'Pre-check submission created',
            'order_id': order.id,
            'file_urls': file_urls or None,
        }, status=status.HTTP_201_CREATED)


class CreateFingerprintingView(APIView):
    """Create Fingerprinting submission with staff + client email notifications."""

    def post(self, request, format=None):
        serializer = FingerprintingSubmissionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        order = serializer.save()

        # Process attribution data
        process_attribution(request, order)

        # Send staff notification
        from ..services.notifications import send_staff_notification, build_order_extra_body

        extra_body = build_order_extra_body(order, 'fingerprinting')
        send_staff_notification(
            order=order,
            order_type='fingerprinting',
            extra_body=extra_body,
        )

        # Send client confirmation email
        from django.template.loader import render_to_string
        from django.core.mail import send_mail
        from django.conf import settings as django_settings

        html_content = render_to_string('emails/fingerprinting_confirmation.html', {
            'name': order.name,
            'preferred_date': order.preferred_date,
            'preferred_time': order.preferred_time,
            'service_location': order.service_location,
            'address': order.address or '',
        })
        try:
            send_mail(
                subject='Fingerprinting Appointment — DC Mobile Notary',
                message='Your fingerprinting appointment has been received.',
                from_email=getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'support@dcmobilenotary.net'),
                recipient_list=[order.email],
                html_message=html_content,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send client email for fingerprinting %s", order.id)

        return Response({
            'message': 'Fingerprinting submission created',
            'order_id': order.id,
        }, status=status.HTTP_201_CREATED)
