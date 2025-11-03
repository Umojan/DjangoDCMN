# orders/tasks.py
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder, MarriageOrder, \
    I9VerificationOrder, QuoteRequest
from .zoho_sync import (
    sync_fbi_order_to_zoho,
    sync_embassy_order_to_zoho,
    sync_translation_order_to_zoho,
    sync_apostille_order_to_zoho,
    sync_marriage_order_to_zoho,
    sync_i9_order_to_zoho, sync_quote_request_to_zoho,
    update_record_fields,
)
from .models import Track
from .utils import service_label


@shared_task
def test_celery_task():
    print("Celery is working!")
    return "Hello from Celery"


@shared_task
def sync_order_to_zoho_task(order_id, order_type):
    try:
        if order_type == "fbi":
            order = FbiApostilleOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_fbi_order_to_zoho(order)
        elif order_type == "embassy":
            order = EmbassyLegalizationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_embassy_order_to_zoho(order)
        elif order_type == "apostille":
            order = ApostilleOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_apostille_order_to_zoho(order)
        elif order_type == "translation":
            order = TranslationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_translation_order_to_zoho(order)
        elif order_type == "marriage":
            order = MarriageOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_marriage_order_to_zoho(order)
        elif order_type == "I-9":
            order = I9VerificationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_i9_order_to_zoho(order)
        elif order_type == "quote":
            order = QuoteRequest.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_quote_request_to_zoho(order)
    except Exception as e:
        print(f"[Celery Task Error] Failed to sync {order_type} order #{order_id} to Zoho: {e}")


@shared_task
def write_tracking_id_to_zoho_task(module_name: str, record_id: str, tracking_id: str) -> bool:
    """Persist TID to Zoho record using configured custom field name.
    Adjust the field key below to your Zoho module custom field.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Example: custom field API name 'Tracking_ID'
    fields = {"Tracking_ID": tracking_id}
    logger.info(f"[Celery] write_tracking_id_to_zoho_task: module={module_name}, record_id={record_id}, tid={tracking_id}")
    
    try:
        ok = update_record_fields(module_name, record_id, fields)
        if not ok:
            logger.error(f"[Celery] ❌ Failed to write Tracking_ID={tracking_id} to Zoho {module_name}/{record_id}")
        else:
            logger.info(f"[Celery] ✅ Successfully wrote Tracking_ID={tracking_id} to Zoho {module_name}/{record_id}")
        return ok
    except Exception as e:
        logger.exception(f"[Celery] Exception writing Tracking_ID={tracking_id} to {module_name}/{record_id}: {e}")
        return False


@shared_task
def send_tracking_email_task(tid: str, stage_code: str):
    track = Track.objects.filter(tid=tid).first()
    if not track or not track.email:
        return

    svc = service_label(track.service)
    subject = "Order update"
    if stage_code == 'created':
        subject = f"We received your documents — {svc}"
    elif stage_code == 'delivered':
        subject = f"Delivered — {svc}"
    elif stage_code in (
        'notarized', 'submitted', 'processed_dos', 'processed_state',
        'translated', 'quality_approved'
    ):
        subject = f"In Progress — {svc}"

    url = f"{settings.BASE_URL}/track/{track.tid}"
    body = f"Update: {svc} — {stage_code}. View tracking: {url}"
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [track.email], fail_silently=True)
    except Exception:
        pass