# orders/services/whatconverts_zoho.py
"""
Zoho CRM sync for WhatConverts phone leads.
"""

import logging
from typing import Dict, Optional
from .attribution import build_zoho_attribution_payload, SOURCE_CATEGORIES

logger = logging.getLogger(__name__)


def sync_phone_lead_to_zoho(phone_lead: 'PhoneCallLead') -> bool:
    """
    Sync phone call lead to Zoho CRM.

    Creates:
    1. Lead/Deal in appropriate module (FBI, Marriage, etc.) with stage "Phone Call Received"
    2. Lead Attribution Record linked to the lead

    Args:
        phone_lead: PhoneCallLead instance

    Returns:
        True if successful, False otherwise
    """
    from ..zoho_client import ZohoCRMClient

    try:
        client = ZohoCRMClient()

        # Build lead payload
        lead_payload = build_zoho_lead_payload(phone_lead)

        # Determine target module - fallback to Get a Quote
        if phone_lead.zoho_module:
            zoho_module = phone_lead.zoho_module
        else:
            zoho_module = 'Get_A_Quote_Leads'  # Default to Get a Quote if service unknown
            logger.info(f"⚠️ Service not detected, defaulting to Get_A_Quote_Leads module")

        logger.info(f"📤 Syncing phone lead {phone_lead.id} to Zoho module: {zoho_module}")
        logger.info(f"   Payload: {lead_payload}")

        # Create lead in Zoho
        response = client.create_record(zoho_module, lead_payload)

        if not response or not response.get('data'):
            logger.error(f"❌ Failed to create lead in Zoho: {response}")
            return False

        # Extract lead ID
        lead_id = response['data'][0]['details']['id']
        phone_lead.zoho_lead_id = lead_id
        phone_lead.zoho_synced = True
        phone_lead.save()

        logger.info(f"✅ Created lead in Zoho {zoho_module}: {lead_id}")

        # Create Attribution Record
        attribution_id = create_attribution_record(phone_lead, lead_id, zoho_module, client)

        if attribution_id:
            phone_lead.zoho_attribution_id = attribution_id
            phone_lead.save()
            logger.info(f"✅ Created attribution record: {attribution_id}")

        return True

    except Exception as e:
        logger.error(f"❌ Error syncing phone lead {phone_lead.id} to Zoho: {e}", exc_info=True)
        return False


def build_zoho_lead_payload(phone_lead: 'PhoneCallLead') -> Dict:
    """
    Build Zoho lead payload from PhoneCallLead.

    Args:
        phone_lead: PhoneCallLead instance

    Returns:
        Dictionary ready for Zoho API
    """
    # Split name into first/last
    name_parts = phone_lead.contact_name.split(' ', 1) if phone_lead.contact_name else ['', '']
    first_name = name_parts[0] if len(name_parts) > 0 else 'Phone'
    last_name = name_parts[1] if len(name_parts) > 1 else 'Lead'

    # Normalize source for Source_Category
    source_lower = phone_lead.source.lower() if phone_lead.source else 'direct'
    source_category = SOURCE_CATEGORIES.get(source_lower, 'Other')

    # Build description with call details
    description_parts = []
    if phone_lead.lead_summary:
        description_parts.append(f"AI Summary: {phone_lead.lead_summary}")
    if phone_lead.intent:
        description_parts.append(f"Intent: {phone_lead.intent}")
    if phone_lead.sentiment:
        description_parts.append(f"Sentiment: {phone_lead.sentiment}")
    if phone_lead.spotted_keywords:
        description_parts.append(f"Keywords: {phone_lead.spotted_keywords}")
    if phone_lead.call_recording_url:
        description_parts.append(f"Recording: {phone_lead.call_recording_url}")

    description = '\n\n'.join(description_parts) if description_parts else 'Phone call lead from WhatConverts'

    payload = {
        'First_Name': first_name,
        'Last_Name': last_name,
        'Email': phone_lead.contact_email,
        'Phone': phone_lead.contact_phone,
        'Company': phone_lead.contact_company or 'N/A',
        'Lead_Status': 'Phone Call Received',  # ← Your new stage!
        'Lead_Source': source_category,
        'Description': description,
        'City': phone_lead.city,
        'State': phone_lead.state,
        'Zip_Code': phone_lead.zip_code,
        'Country': phone_lead.country,
    }

    # Add service-specific fields
    if phone_lead.detected_service:
        payload['Service_Type'] = phone_lead.detected_service.title()

    # Add lead score if available
    if phone_lead.lead_score is not None:
        payload['Rating'] = calculate_rating(phone_lead.lead_score)

    # Remove None/empty string values (preserve 0 and False)
    payload = {k: v for k, v in payload.items() if v is not None and v != ''}

    return payload


def calculate_rating(lead_score: int) -> str:
    """Convert WhatConverts lead score to Zoho rating."""
    if lead_score >= 80:
        return 'Hot'
    elif lead_score >= 50:
        return 'Warm'
    else:
        return 'Cold'


def create_attribution_record(
    phone_lead: 'PhoneCallLead',
    zoho_lead_id: str,
    zoho_module: str,
    client: 'ZohoCRMClient'
) -> Optional[str]:
    """
    Create Lead Attribution Record in Zoho.

    Args:
        phone_lead: PhoneCallLead instance
        zoho_lead_id: Zoho lead/deal ID
        zoho_module: Module name where lead was created
        client: ZohoCRMClient instance

    Returns:
        Attribution record ID or None
    """
    try:
        # Build attribution payload using existing function
        from .whatconverts import build_attribution_from_phone_lead
        attribution_data = build_attribution_from_phone_lead(phone_lead)

        # Build Zoho payload
        zoho_payload = build_zoho_attribution_payload(
            attribution_data=attribution_data,
            lead_name=phone_lead.contact_name or 'Phone Lead'
        )

        if not zoho_payload:
            logger.warning("Could not build attribution payload")
            return None

        # Link to lead
        zoho_payload['Attribution_Record'] = zoho_lead_id

        logger.info(f"📤 Creating attribution record for lead {zoho_lead_id}")

        response = client.create_record('Lead_Attribution_Records', zoho_payload)

        if response and response.get('data'):
            attribution_id = response['data'][0]['details']['id']
            return attribution_id

        logger.error(f"❌ Failed to create attribution record: {response}")
        return None

    except Exception as e:
        logger.error(f"❌ Error creating attribution record: {e}", exc_info=True)
        return None


def update_order_stage_to_received(order_type: str, order_id: int) -> bool:
    """
    Update matched order stage to "Order Received" when form is submitted after phone call.

    Args:
        order_type: Type of order (fbi, marriage, etc.)
        order_id: Order ID

    Returns:
        True if successful
    """
    from ..zoho_client import ZohoCRMClient
    from ..models import PhoneCallLead

    try:
        # Find the PhoneCallLead that was matched with this order
        phone_lead = PhoneCallLead.objects.filter(
            matched_order_type=order_type,
            matched_order_id=order_id,
            zoho_lead_id__gt='',
        ).first()

        if not phone_lead:
            logger.warning(f"No matched phone lead with Zoho ID for {order_type}:{order_id}")
            return False

        zoho_module = phone_lead.zoho_module
        zoho_id = phone_lead.zoho_lead_id

        if not zoho_module or not zoho_id:
            logger.warning(f"Phone lead {phone_lead.id} missing Zoho module or ID")
            return False

        # Update stage in Zoho
        client = ZohoCRMClient()
        update_payload = {
            'Stage': 'Order Received'
        }

        response = client.update_record(zoho_module, zoho_id, update_payload)

        if response and response.get('data'):
            logger.info(f"✅ Updated {order_type} order {order_id} to 'Order Received' stage in Zoho")
            return True

        logger.error(f"❌ Failed to update order stage: {response}")
        return False

    except Exception as e:
        logger.error(f"❌ Error updating order stage: {e}", exc_info=True)
        return False
