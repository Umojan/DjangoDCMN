# orders/services/order_processing.py
"""Main order processing pipeline."""

from .files import save_file_attachments, build_file_links
from .notifications import send_staff_notification, build_order_extra_body
from .tracking import create_order_tracking
from ..tasks import sync_order_to_zoho_task, send_tracking_email_task
import logging

logger = logging.getLogger(__name__)


def process_new_order(
    request,
    order,
    model_class,
    order_type: str,
    sync_to_zoho: bool = True,
    create_tracking: bool = True,
    send_notification: bool = True,
    send_welcome_email: bool = True,
) -> dict:
    """
    Full order processing pipeline:
    1. Save file attachments
    2. Create tracking record
    3. Sync to Zoho
    4. Send staff notification
    5. Send welcome tracking email
    
    Args:
        request: Django request object
        order: Order instance
        model_class: Model class of the order
        order_type: Type key (embassy, apostille, translation, i9, quote)
        sync_to_zoho: Whether to sync to Zoho CRM
        create_tracking: Whether to create tracking record
        send_notification: Whether to send staff email
        send_welcome_email: Whether to send tracking welcome email
    
    Returns:
        Dict with processing results
    """
    result = {
        'order_id': order.id,
        'file_urls': None,
        'tracking_id': None,
    }
    
    # 1. Save file attachments
    file_urls = save_file_attachments(request, model_class, order)
    if file_urls:
        result['file_urls'] = file_urls
    
    # 2. Create tracking record (if applicable)
    tid = None
    if create_tracking:
        tid = create_order_tracking(order, order_type)
        if tid:
            result['tracking_id'] = tid
    
    # 3. Sync to Zoho (async)
    if sync_to_zoho:
        try:
            if tid:
                sync_order_to_zoho_task.delay(order.id, order_type, tracking_id=tid)
            else:
                sync_order_to_zoho_task.delay(order.id, order_type)
            logger.info(f"Queued Zoho sync for {order_type} order {order.id}")
        except Exception as e:
            logger.exception(f"Failed to queue Zoho sync for {order_type} order {order.id}: {e}")
    
    # 4. Send staff notification
    if send_notification:
        file_links = build_file_links(request, order, html=False)
        extra_body = build_order_extra_body(order, order_type)
        send_staff_notification(
            order=order,
            order_type=order_type,
            extra_body=extra_body,
            file_links=file_links,
        )
    
    # 5. Send welcome tracking email
    if send_welcome_email and tid:
        try:
            send_tracking_email_task.delay(tid, 'created')
            logger.info(f"Queued tracking email for {order_type} order {order.id}")
        except Exception as e:
            logger.exception(f"Failed to queue tracking email for {order_type} order {order.id}: {e}")
    
    return result
