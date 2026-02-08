# orders/services/phone_lead_matcher.py
"""
Phone lead matching and updating logic.
Used when web form is submitted after phone call to update existing lead.
"""

import logging
from typing import Optional, Dict, Any
from django.db.models import Q

logger = logging.getLogger(__name__)


def find_phone_lead_for_order(phone: str, service_type: str) -> Optional['PhoneCallLead']:
    """
    Find matching PhoneCallLead by phone number and service type.

    Only searches within the same pipeline (service type).

    Args:
        phone: Phone number from web form
        service_type: Service type (fbi, marriage, embassy, etc.)

    Returns:
        PhoneCallLead instance or None
    """
    from ..models import PhoneCallLead

    if not phone:
        return None

    # Normalize phone (remove spaces, dashes, parentheses)
    normalized_phone = ''.join(c for c in phone if c.isdigit())

    if not normalized_phone or len(normalized_phone) < 10:
        logger.warning(f"Phone number too short after normalization: {phone}")
        return None

    # Get last 10 digits for matching
    phone_last_10 = normalized_phone[-10:]

    logger.info(f"🔍 Searching for phone lead: phone={phone_last_10}, service={service_type}")

    # Search for phone lead with matching phone and service
    query = Q(contact_phone__icontains=phone_last_10)

    # Filter by service if provided
    if service_type:
        query &= Q(detected_service=service_type)

    # Get most recent matching lead
    phone_lead = PhoneCallLead.objects.filter(query).order_by('-created_at').first()

    if phone_lead:
        logger.info(f"✅ Found matching phone lead: {phone_lead.id} (created {phone_lead.created_at})")
        return phone_lead

    logger.info(f"❌ No matching phone lead found")
    return None


def update_phone_lead_with_form_data(
    phone_lead: 'PhoneCallLead',
    order_data: Dict[str, Any],
    order_type: str,
    order_id: int
) -> bool:
    """
    Update existing PhoneCallLead with data from web form submission.

    Preserves WhatConverts attribution data (source, medium, campaign, etc.)
    Updates contact info and marks as matched with form.

    Args:
        phone_lead: PhoneCallLead instance to update
        order_data: Dictionary with form data (name, email, address, etc.)
        order_type: Type of order (fbi, marriage, etc.)
        order_id: Order ID

    Returns:
        True if updated successfully
    """
    try:
        logger.info("=" * 80)
        logger.info(f"🔄 Updating phone lead {phone_lead.id} with form data")
        logger.info(f"   Order: {order_type} #{order_id}")
        logger.info("=" * 80)

        # Update contact information (preserve if form data is empty)
        if order_data.get('name'):
            old_name = phone_lead.contact_name
            phone_lead.contact_name = order_data['name']
            logger.info(f"   Name: {old_name} → {order_data['name']}")

        if order_data.get('email'):
            old_email = phone_lead.contact_email
            phone_lead.contact_email = order_data['email']
            logger.info(f"   Email: {old_email} → {order_data['email']}")

        if order_data.get('phone'):
            old_phone = phone_lead.contact_phone
            phone_lead.contact_phone = order_data['phone']
            logger.info(f"   Phone: {old_phone} → {order_data['phone']}")

        # Update location if provided
        if order_data.get('city'):
            phone_lead.city = order_data['city']
        if order_data.get('state'):
            phone_lead.state = order_data['state']
        if order_data.get('country'):
            phone_lead.country = order_data['country']

        # Mark as matched with form
        phone_lead.matched_with_form = True
        phone_lead.matched_order_type = order_type
        phone_lead.matched_order_id = order_id

        phone_lead.save()

        logger.info(f"✅ Phone lead {phone_lead.id} updated with form data")
        logger.info(f"   WhatConverts attribution PRESERVED:")
        logger.info(f"   - Source: {phone_lead.source}")
        logger.info(f"   - Medium: {phone_lead.medium}")
        logger.info(f"   - Campaign: {phone_lead.campaign}")
        logger.info(f"   - GCLID: {phone_lead.gclid}")
        logger.info(f"   - Lead Score: {phone_lead.lead_score}")
        logger.info("=" * 80)

        return True

    except Exception as e:
        logger.error(f"❌ Error updating phone lead {phone_lead.id}: {e}", exc_info=True)
        return False


def update_zoho_lead_stage(phone_lead: 'PhoneCallLead', new_stage: str = 'Order Received') -> bool:
    """
    Update Zoho lead stage when form is submitted after phone call.

    Args:
        phone_lead: PhoneCallLead instance
        new_stage: New stage name (default: "Order Received")

    Returns:
        True if successful
    """
    from ..zoho_client import ZohoCRMClient

    if not phone_lead.zoho_lead_id:
        logger.warning(f"Phone lead {phone_lead.id} doesn't have Zoho lead ID")
        return False

    if not phone_lead.zoho_module:
        logger.warning(f"Phone lead {phone_lead.id} doesn't have Zoho module")
        return False

    try:
        client = ZohoCRMClient()

        update_payload = {
            'Stage': new_stage
        }

        logger.info(f"📤 Updating Zoho lead {phone_lead.zoho_lead_id} in {phone_lead.zoho_module}")
        logger.info(f"   New stage: {new_stage}")

        response = client.update_record(
            phone_lead.zoho_module,
            phone_lead.zoho_lead_id,
            update_payload
        )

        if response and response.get('data'):
            logger.info(f"✅ Updated Zoho lead stage to '{new_stage}'")
            return True

        logger.error(f"❌ Failed to update Zoho lead stage: {response}")
        return False

    except Exception as e:
        logger.error(f"❌ Error updating Zoho lead stage: {e}", exc_info=True)
        return False


def process_order_with_phone_lead_check(
    order_instance: Any,
    order_type: str,
    order_data: Dict[str, Any]
) -> Optional['PhoneCallLead']:
    """
    Main function to check for phone lead and update if found.

    Call this after creating an order from web form.

    Args:
        order_instance: Order model instance (FbiApostilleOrder, MarriageOrder, etc.)
        order_type: Service type (fbi, marriage, embassy, etc.)
        order_data: Dictionary with form data

    Returns:
        PhoneCallLead if found and updated, None otherwise
    """
    phone = order_data.get('phone')

    if not phone:
        logger.info("⏭️ No phone number in order, skipping phone lead check")
        return None

    # Find matching phone lead
    phone_lead = find_phone_lead_for_order(phone, order_type)

    if not phone_lead:
        logger.info("⏭️ No matching phone lead found")
        return None

    # Update phone lead with form data
    updated = update_phone_lead_with_form_data(
        phone_lead,
        order_data,
        order_type,
        order_instance.id
    )

    if not updated:
        logger.warning("⚠️ Failed to update phone lead with form data")
        return phone_lead

    # Update Zoho lead stage
    stage_updated = update_zoho_lead_stage(phone_lead, 'Order Received')

    if stage_updated:
        logger.info(f"✅ Complete: Phone lead matched, updated, and Zoho synced")
    else:
        logger.warning(f"⚠️ Phone lead updated but Zoho stage update failed")

    return phone_lead
