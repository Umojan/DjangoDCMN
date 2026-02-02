# orders/services/files.py
"""File attachment handling utilities."""

from django.contrib.contenttypes.models import ContentType
from ..models import FileAttachment
import logging

logger = logging.getLogger(__name__)


def save_file_attachments(request, model_class, order) -> list[str]:
    """
    Save uploaded files from request and attach to order.
    
    Args:
        request: Django request object
        model_class: Model class (e.g., FbiApostilleOrder)
        order: Order instance to attach files to
    
    Returns:
        List of absolute URLs to uploaded files
    """
    file_urls = []
    if not request.FILES:
        return file_urls
    
    ct = ContentType.objects.get_for_model(model_class)
    for f in request.FILES.getlist('files'):
        try:
            attachment = FileAttachment.objects.create(
                content_type=ct,
                object_id=order.id,
                file=f
            )
            file_urls.append(request.build_absolute_uri(attachment.file.url))
        except Exception as e:
            logger.exception(f"Failed to save file attachment for order {order.id}: {e}")
    
    return file_urls


def build_file_links(request, order, html: bool = False) -> str:
    """
    Build formatted file links string from order attachments.
    
    Args:
        request: Django request object
        order: Order instance with file_attachments relation
        html: If True, return HTML list items; otherwise plain text
    
    Returns:
        Formatted string with file links or 'None'
    """
    attachments = order.file_attachments.all()
    
    if not attachments:
        return "<li>No files attached</li>" if html else "None"
    
    if html:
        return "".join([
            f'<li><a href="{request.build_absolute_uri(f.file.url)}">{f.file.name}</a></li>'
            for f in attachments
        ])
    else:
        return "".join([
            f"📎 {request.build_absolute_uri(f.file.url)}\n"
            for f in attachments
        ])
