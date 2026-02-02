# orders/services/notifications.py
"""Email notification utilities for orders."""

from django.conf import settings
from django.core.mail import EmailMessage
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


# Email configuration per order type
ORDER_EMAIL_CONFIG = {
    'embassy': {
        'subject_prefix': '📄 New Embassy Legalization Order',
        'thread_prefix': 'embassy-orders-thread',
        'message_prefix': 'embassy-order',
    },
    'apostille': {
        'subject_prefix': '📄 New Apostille Order',
        'thread_prefix': 'apostille-orders-thread',
        'message_prefix': 'apostille-order',
    },
    'translation': {
        'subject_prefix': '📄 New Translation Order',
        'thread_prefix': 'translation-orders-thread',
        'message_prefix': 'translation-order',
    },
    'i9': {
        'subject_prefix': '📄 New I-9 Verification Order',
        'thread_prefix': 'i9-orders-thread',
        'message_prefix': 'i9-order',
    },
    'quote': {
        'subject_prefix': '❓ New Quote Request',
        'thread_prefix': 'quote-request-thread',
        'message_prefix': 'quote-request',
    },
    'fbi': {
        'subject_prefix': '✅ New Paid FBI Apostille Order',
        'thread_prefix': 'fbi-orders-thread',
        'message_prefix': 'order',
    },
    'marriage': {
        'subject_prefix': '✅ New Paid Marriage Certificate Order',
        'thread_prefix': 'marriage-orders-thread',
        'message_prefix': 'marriage-order',
    },
}


def send_staff_notification(
    order,
    order_type: str,
    extra_body: str = '',
    file_links: str = '',
    paid: bool = False
) -> bool:
    """
    Send notification email to staff about new order.
    
    Args:
        order: Order instance (must have id, name, email, phone attributes)
        order_type: Type key from ORDER_EMAIL_CONFIG
        extra_body: Additional text to include in email body
        file_links: Formatted file links string
        paid: Whether the order is paid (changes subject)
    
    Returns:
        True if email sent successfully, False otherwise
    """
    config = ORDER_EMAIL_CONFIG.get(order_type)
    if not config:
        logger.error(f"Unknown order type for notification: {order_type}")
        return False
    
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    thread_id = f"<{config['thread_prefix']}-{today_str}@dcmobilenotary.com>"
    
    # Build basic info
    base_info = (
        f"Name: {order.name}\n"
        f"Email: {order.email}\n"
        f"Phone: {order.phone}\n"
    )
    
    # Add address if available
    if hasattr(order, 'address') and order.address:
        base_info += f"Address: {order.address}\n"
    
    email_body = f"{config['subject_prefix'].split(' ', 1)[1]} submitted! Order ID: {order.id}\n\n"
    email_body += base_info
    
    if extra_body:
        email_body += f"\n{extra_body}\n"
    
    if file_links:
        email_body += f"\nFiles:\n{file_links}"
    
    try:
        email = EmailMessage(
            subject=f"{config['subject_prefix']} — {today_str}",
            body=email_body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "support@dcmobilenotary.net"),
            to=settings.EMAIL_OFFICE_RECEIVER,
            headers={
                "Message-ID": f"<{config['message_prefix']}-{order.id}@dcmobilenotary.com>",
                "In-Reply-To": thread_id,
                "References": thread_id,
            }
        )
        email.send()
        logger.info(f"✅ Staff notification sent for {order_type} order {order.id}")
        return True
    except Exception as e:
        logger.exception(f"Failed to send staff notification for {order_type} order {order.id}: {e}")
        return False


def build_order_extra_body(order, order_type: str) -> str:
    """
    Build extra body content based on order type.
    
    Args:
        order: Order instance
        order_type: Type of order
    
    Returns:
        Formatted extra body string
    """
    extra = ""
    
    if order_type == 'embassy':
        extra = (
            f"Document Type: {order.document_type}\n"
            f"Country: {order.country}\n"
            f"Comments: {order.comments or ''}"
        )
    
    elif order_type == 'apostille':
        extra = (
            f"Documents Type: {order.type}\n"
            f"Country: {order.country}\n"
            f"Service Type: {order.service_type}\n"
        )
        if order.service_type == "My Address" and order.address:
            extra += f"Address: {order.address}\n"
        if order.comments:
            extra += f"Comments: {order.comments}"
    
    elif order_type == 'translation':
        extra = (
            f"Languages: {order.languages}\n"
            f"Comments: \n{order.comments or ''}"
        )
    
    elif order_type == 'i9':
        extra = (
            f"Date: {order.appointment_date}\n"
            f"Time: {order.appointment_time}\n\n"
            f"Comments: \n{order.comments or 'None'}"
        )
    
    elif order_type == 'quote':
        extra = (
            f"Date: {order.appointment_date}, Time: {order.appointment_time}\n"
            f"Number of documents: {order.number}\n"
            f"Services: {order.services}\n\n"
            f"Message: \n{order.comments or ''}"
        )
    
    return extra
