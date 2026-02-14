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

        # Stage field name depends on module
        stage_field = _get_stage_field(phone_lead.zoho_module)
        update_payload = {
            stage_field: new_stage
        }

        logger.info(f"📤 Updating Zoho lead {phone_lead.zoho_lead_id} in {phone_lead.zoho_module}")
        logger.info(f"   {stage_field}: {new_stage}")

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


def _get_stage_field(zoho_module: str) -> str:
    """Return the correct stage/status field name for a Zoho module."""
    stage_field_map = {
        'Deals': 'Status',
        'Embassy_Legalization': 'Status',
        'Apostille_Services': 'Status',
        'Translation_Services': 'Translation_Status',
        'Triple_Seal_Apostilles': 'Stage',
        'I_9_Verification': 'Stage',
        'Get_A_Quote_Leads': 'GET_A_QUOTE_LEADS',
    }
    return stage_field_map.get(zoho_module, 'Stage')


def _get_form_stage(zoho_module: str) -> str:
    """Return the correct 'order received' stage value per module (from zoho_sync.py)."""
    form_stage_map = {
        'Deals': 'Order Received',
        'Embassy_Legalization': 'Order Received',
        'Apostille_Services': 'Client placed the request',
        'Translation_Services': 'Client Placed Request',
        'Triple_Seal_Apostilles': 'Order Received',
        'I_9_Verification': 'Order Received',
        'Get_A_Quote_Leads': 'Order Received',
    }
    return form_stage_map.get(zoho_module, 'Order Received')


def update_zoho_lead_with_order_data(
    phone_lead: 'PhoneCallLead',
    order_data: Dict[str, Any],
    new_stage: str = None
) -> bool:
    """
    Update Zoho lead with form data. Optionally update stage.

    This updates:
    - Client Name (from form)
    - Email (from form)
    - Location (from form)
    - Stage ONLY if new_stage is provided (None = don't touch stage)

    Args:
        phone_lead: PhoneCallLead instance
        order_data: Dictionary with form data
        new_stage: New stage name, or None to leave stage untouched

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
        zoho_module = phone_lead.zoho_module

        update_payload = {}

        # Only update stage if explicitly requested
        if new_stage is not None:
            stage_field = _get_stage_field(zoho_module)
            update_payload[stage_field] = new_stage

        # Name/Email field names depend on module
        if order_data.get('name'):
            if zoho_module == 'Deals':
                update_payload['Name1'] = order_data['name']
            elif zoho_module == 'Translation_Services':
                update_payload['Client_Name1'] = order_data['name']
            else:
                update_payload['Client_Name'] = order_data['name']

        if order_data.get('email'):
            if zoho_module == 'Deals':
                update_payload['Email_1'] = order_data['email']
            elif zoho_module in ('Triple_Seal_Apostilles', 'I_9_Verification', 'Get_A_Quote_Leads'):
                update_payload['Client_Email'] = order_data['email']
            else:
                update_payload['Email'] = order_data['email']

        # Add location if provided
        if order_data.get('city'):
            update_payload['City'] = order_data['city']
        if order_data.get('state'):
            update_payload['State'] = order_data['state']
        if order_data.get('country'):
            update_payload['Country'] = order_data['country']

        logger.info(f"📤 Updating Zoho lead {phone_lead.zoho_lead_id} in {zoho_module}")
        logger.info(f"   Update payload: {update_payload}")

        response = client.update_record(
            phone_lead.zoho_module,
            phone_lead.zoho_lead_id,
            update_payload
        )

        if response and response.get('data'):
            logger.info(f"✅ Updated Zoho lead with form data and stage '{new_stage}'")
            return True

        logger.error(f"❌ Failed to update Zoho lead: {response}")
        return False

    except Exception as e:
        logger.error(f"❌ Error updating Zoho lead: {e}", exc_info=True)
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

    # NOTE: We do NOT set order.zoho_synced = True here.
    # That flag should only be True after data actually reaches Zoho.
    # Instead, Celery task checks for matched phone lead to decide CREATE vs UPDATE.
    # zoho_synced will be set to True by Celery after successful Zoho operation.

    # Check current stage in Zoho before updating
    # Only advance from "Phone Call Received" → "Order Received"
    # If manager already moved it further — don't roll back
    if phone_lead.zoho_lead_id and phone_lead.zoho_module:
        from ..zoho_sync import get_record_by_id

        stage_field = _get_stage_field(phone_lead.zoho_module)
        record = get_record_by_id(phone_lead.zoho_module, phone_lead.zoho_lead_id, [stage_field])
        current_stage = record.get(stage_field) if record else None

        if current_stage == 'Phone Call Received':
            target_stage = _get_form_stage(phone_lead.zoho_module)
            zoho_updated = update_zoho_lead_with_order_data(phone_lead, order_data, new_stage=target_stage)
            if zoho_updated:
                logger.info(f"✅ Moved phone lead from 'Phone Call Received' → '{target_stage}'")
            else:
                logger.warning(f"⚠️ Failed to update Zoho lead stage")
        else:
            zoho_updated = update_zoho_lead_with_order_data(phone_lead, order_data, new_stage=None)
            logger.info(f"⏭️ Phone lead already at '{current_stage}', stage untouched, contact data updated")
    else:
        logger.info(f"⏭️ Phone lead not synced to Zoho yet, skipping Zoho update")

    return phone_lead
