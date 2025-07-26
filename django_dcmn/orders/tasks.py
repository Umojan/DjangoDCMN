# orders/tasks.py
from celery import shared_task
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder, MarriageOrder, \
    I9VerificationOrder, QuoteRequest
from .zoho_sync import (
    sync_fbi_order_to_zoho,
    sync_embassy_order_to_zoho,
    sync_translation_order_to_zoho,
    sync_apostille_order_to_zoho,
    sync_marriage_order_to_zoho,
    sync_i9_order_to_zoho, sync_quote_request_to_zoho
)


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