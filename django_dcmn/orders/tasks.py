# orders/tasks.py
from celery import shared_task
from .models import FbiApostilleOrder
from .zoho_sync import sync_fbi_order_to_zoho

@shared_task
def test_celery_task():
    print("Celery is working!")
    return "Hello from Celery"


@shared_task
def sync_order_to_zoho_task(order_id):
    try:
        order = FbiApostilleOrder.objects.get(id=order_id)
        if not order.zoho_synced:
            sync_fbi_order_to_zoho(order)
    except Exception as e:
        print(f"[Celery Task Error] Failed to sync order #{order_id} to Zoho: {e}")