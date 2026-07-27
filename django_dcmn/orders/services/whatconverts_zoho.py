# orders/services/whatconverts_zoho.py
"""
Zoho CRM sync for WhatConverts phone leads.
"""

import logging
from typing import Dict

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
    from .whatconverts import build_attribution_from_phone_lead
    from ..zoho_errors import ZohoPermanentError
    from ..zoho_sync import (
        EXTERNAL_ORDER_KEY_FIELD,
        ZOHO_ATTRIBUTION_MODULE,
        create_attribution_record as create_order_attribution_record,
        create_record_idempotently,
        get_attribution_external_key,
        get_or_create_contact_id,
        get_order_external_key,
        update_record_fields,
    )

    zoho_module = phone_lead.zoho_module or 'Get_A_Quote_Leads'
    external_key = get_order_external_key(phone_lead)
    lead_payload = build_zoho_lead_payload(phone_lead)
    lead_payload[EXTERNAL_ORDER_KEY_FIELD] = external_key

    logger.info(
        '[Zoho Phone] Syncing phone lead %s to %s',
        phone_lead.id,
        zoho_module,
    )

    if phone_lead.zoho_lead_id:
        lead_id = phone_lead.zoho_lead_id
        update_record_fields(
            zoho_module,
            lead_id,
            {EXTERNAL_ORDER_KEY_FIELD: external_key},
        )
    else:
        lead_id = create_record_idempotently(
            module_name=zoho_module,
            record=lead_payload,
            unique_field=EXTERNAL_ORDER_KEY_FIELD,
            unique_value=external_key,
        )
        phone_lead.zoho_module = zoho_module
        phone_lead.zoho_lead_id = lead_id
        phone_lead.save(update_fields=['zoho_module', 'zoho_lead_id', 'updated_at'])

    contact_id = get_or_create_contact_id(
        phone_lead.contact_name,
        phone_lead.contact_email,
        phone_lead.contact_phone,
    )
    lookup_field = CONTACT_LOOKUP_FIELD.get(zoho_module)
    if contact_id and lookup_field:
        update_record_fields(
            zoho_module,
            lead_id,
            {lookup_field: {'id': contact_id}},
        )

    attribution_external_key = get_attribution_external_key(phone_lead)
    if phone_lead.zoho_attribution_id:
        attribution_id = phone_lead.zoho_attribution_id
        update_record_fields(
            ZOHO_ATTRIBUTION_MODULE,
            attribution_id,
            {EXTERNAL_ORDER_KEY_FIELD: attribution_external_key},
        )
    else:
        attribution_id = create_order_attribution_record(
            build_attribution_from_phone_lead(phone_lead),
            lead_name=phone_lead.contact_name or 'Phone Lead',
            external_key=attribution_external_key,
        )
        if not attribution_id:
            raise ZohoPermanentError(
                f'No attribution payload for phone lead #{phone_lead.id}'
            )
        phone_lead.zoho_attribution_id = attribution_id
        phone_lead.save(update_fields=['zoho_attribution_id', 'updated_at'])

    update_record_fields(
        zoho_module,
        lead_id,
        {'Attribution_Record': str(attribution_id)},
    )

    phone_lead.zoho_synced = True
    phone_lead.zoho_module = zoho_module
    phone_lead.zoho_lead_id = lead_id
    phone_lead.zoho_attribution_id = attribution_id
    phone_lead.save(
        update_fields=[
            'zoho_synced',
            'zoho_module',
            'zoho_lead_id',
            'zoho_attribution_id',
            'updated_at',
        ]
    )
    logger.info('[Zoho Phone] Synced phone lead %s', phone_lead.id)
    return True


# Module → Contact lookup field name
CONTACT_LOOKUP_FIELD = {
    'Deals': 'Client_Contact',
    'Triple_Seal_Apostilles': 'Client_Contact',
    'I_9_Verification': 'Client_Contact',
    'Embassy_Legalization': 'Client_Contact',
    'Apostille_Services': 'Client_Contact',
    'Translation_Services': 'Client_Contact',
    'Notary_Services': 'Client_Name',
    'Get_A_Quote_Leads': 'Name_of_Client',
}


def build_zoho_lead_payload(phone_lead: 'PhoneCallLead') -> Dict:
    """
    Build Zoho lead payload from PhoneCallLead.
    Adapts field names based on the target Zoho module.

    Field mapping per module (from zoho_sync.py):
    ┌──────────────────────────┬────────────┬─────────────┬───────────┬──────────────┬────────────────────┬──────────────────────┐
    │ Module                   │ Name field │ Client Name │ Email     │ Phone        │ Stage/Status       │ Comments             │
    ├──────────────────────────┼────────────┼─────────────┼───────────┼──────────────┼────────────────────┼──────────────────────┤
    │ Deals (FBI)              │ Deal_Name  │ Name1       │ Email_1   │ Phone        │ Stage              │ Client_Comment       │
    │ Embassy_Legalization     │ Name       │ Client_Name │ Email     │ Phone        │ Status             │ Client_Comment       │
    │ Translation_Services     │ Name       │ Client_Name1│ Email     │ Phone        │ Translation_Status │ Client_Comments      │
    │ Apostille_Services       │ Name       │ Client_Name │ Email     │ Phone_Number │ Status             │ Client_Comments      │
    │ Triple_Seal_Apostilles   │ Name       │ Client_Name │ Client_Email│Client_Phone│ Stage              │ Client_Notes_Comments│
    │ I_9_Verification         │ Name       │ Client_Name │ Client_Email│Client_Phone│ Stage              │ Client_Comments      │
    │ Get_A_Quote_Leads        │ Name       │ Client_Name │ Client_Email│Client_Phone│ GET_A_QUOTE_LEADS  │ Client_Comments      │
    └──────────────────────────┴────────────┴─────────────┴───────────┴──────────────┴────────────────────┴──────────────────────┘

    Args:
        phone_lead: PhoneCallLead instance

    Returns:
        Dictionary ready for Zoho API
    """
    name = phone_lead.contact_name or 'Phone Lead'

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

    zoho_module = phone_lead.zoho_module or ''

    # --- Deals module (FBI) ---
    if zoho_module == 'Deals':
        payload = {
            'Deal_Name': f"FBI Phone Lead — {name}",
            'Name1': name,
            'Email_1': phone_lead.contact_email,
            'Phone': phone_lead.contact_phone,
            'Stage': 'Phone Call Received',
            'Client_Comment': description,
        }

    # --- Embassy_Legalization ---
    elif zoho_module == 'Embassy_Legalization':
        payload = {
            'Name': f"Embassy Phone Lead — {name}",
            'Client_Name': name,
            'Email': phone_lead.contact_email,
            'Phone': phone_lead.contact_phone,
            'Status': 'Phone Call Received',
            'Client_Comment': description,
        }

    # --- Translation_Services ---
    elif zoho_module == 'Translation_Services':
        payload = {
            'Name': f"Translation Phone Lead — {name}",
            'Client_Name1': name,
            'Email': phone_lead.contact_email,
            'Phone': phone_lead.contact_phone,
            'Translation_Status': 'Phone Call Received',
            'Client_Comments': description,
        }

    # --- Apostille_Services ---
    elif zoho_module == 'Apostille_Services':
        payload = {
            'Name': f"Apostille Phone Lead — {name}",
            'Client_Name': name,
            'Email': phone_lead.contact_email,
            'Phone_Number': phone_lead.contact_phone,
            'Status': 'Phone Call Received',
            'Client_Comments': description,
        }

    # --- Triple_Seal_Apostilles (Marriage) ---
    elif zoho_module == 'Triple_Seal_Apostilles':
        payload = {
            'Name': f"Triple Seal Phone Lead — {name}",
            'Client_Name': name,
            'Client_Email': phone_lead.contact_email,
            'Client_Phone': phone_lead.contact_phone,
            'Stage': 'Phone Call Received',
            'Client_Notes_Comments': description,
        }

    # --- I_9_Verification ---
    elif zoho_module == 'I_9_Verification':
        payload = {
            'Name': f"I9 Phone Lead — {name}",
            'Client_Name': name,
            'Client_Email': phone_lead.contact_email,
            'Client_Phone': phone_lead.contact_phone,
            'Stage': 'Phone Call Received',
            'Client_Comments': description,
        }

    # --- Notary_Services ---
    elif zoho_module == 'Notary_Services':
        payload = {
            'Name': f"Notary Phone Lead — {name}",
            'Client_Email': phone_lead.contact_email,
            'Client_Phone_Number': phone_lead.contact_phone,
            'Notary_Stages': 'Phone Call Received',
            'Client_Comments': description,
        }

    # --- Get_A_Quote_Leads ---
    elif zoho_module == 'Get_A_Quote_Leads':
        payload = {
            'Name': f"Quote Phone Lead — {name}",
            'Client_Name': name,
            'Client_Email': phone_lead.contact_email,
            'Client_Phone': phone_lead.contact_phone,
            'GET_A_QUOTE_LEADS': 'Phone Call Received',
            'Client_Comments': description,
        }

    # --- Fallback for unknown modules → goes to Get_A_Quote_Leads ---
    else:
        payload = {
            'Name': f"Phone Lead — {name}",
            'Client_Name': name,
            'Client_Email': phone_lead.contact_email,
            'Client_Phone': phone_lead.contact_phone,
            'GET_A_QUOTE_LEADS': 'Phone Call Received',
            'Client_Comments': description,
        }

    # Common fields for all modules
    if phone_lead.city:
        payload['City'] = phone_lead.city
    if phone_lead.state:
        payload['State'] = phone_lead.state
    if phone_lead.country:
        payload['Country'] = phone_lead.country

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

        # Update stage in Zoho — field name depends on module
        # Deals/Marriage/I9 use 'Stage', Embassy/Apostille use 'Status', Translation uses 'Translation_Status'
        stage_field_map = {
            'Deals': 'Stage',
            'Embassy_Legalization': 'Status',
            'Apostille_Services': 'Status',
            'Translation_Services': 'Translation_Status',
            'Triple_Seal_Apostilles': 'Stage',
            'I_9_Verification': 'Stage',
        }
        stage_field = stage_field_map.get(zoho_module, 'Stage')

        client = ZohoCRMClient()
        update_payload = {
            stage_field: 'Order Received'
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
