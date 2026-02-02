# orders/services/tracking.py
"""Tracking record creation utilities."""

from ..models import Track
from ..utils import generate_tid
from ..constants import STAGE_DEFS
import logging

logger = logging.getLogger(__name__)


# Mapping from order_type to service key
ORDER_TYPE_TO_SERVICE = {
    'embassy': 'embassy_legalization',
    'apostille': 'state_apostille',
    'translation': 'translation',
    'fbi': 'fbi_apostille',
    'marriage': 'marriage',
    'i9': None,  # I-9 doesn't have tracking
    'quote': None,  # Quote doesn't have tracking
}


def create_order_tracking(order, order_type: str) -> str | None:
    """
    Create a tracking record for an order.
    
    Args:
        order: Order instance (must have id, name, email attributes)
        order_type: Type of order (embassy, apostille, translation, etc.)
    
    Returns:
        Tracking ID (tid) if created, None otherwise
    """
    service = ORDER_TYPE_TO_SERVICE.get(order_type)
    if not service:
        logger.debug(f"Order type {order_type} does not support tracking")
        return None
    
    # Get start stage for this service
    codes = [d['code'] for d in STAGE_DEFS.get(service, [])]
    start_stage = codes[0] if codes else 'document_received'
    
    tid = generate_tid()
    
    try:
        Track.objects.create(
            tid=tid,
            service=service,
            data={
                'name': order.name,
                'email': order.email,
                'service': service,
                'current_stage': start_stage,
                'order_id': order.id,
                'order_type': order_type
            }
        )
        logger.info(f"Created tracking {tid} for {order_type} order {order.id}")
        return tid
    except Exception as e:
        logger.exception(f"Failed to create Track for {order_type} order {order.id}: {e}")
        return None
