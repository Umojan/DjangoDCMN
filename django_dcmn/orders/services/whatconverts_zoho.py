# orders/services/whatconverts_zoho.py
"""
Zoho CRM sync for WhatConverts phone leads.
"""

import logging
import requests
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
        # If already synced to Zoho — don't create a duplicate record
        # The lead may have been moved further in the pipeline by a manager
        if phone_lead.zoho_synced and phone_lead.zoho_lead_id:
            logger.info(f"⏭️ Phone lead {phone_lead.id} already synced to Zoho (lead_id={phone_lead.zoho_lead_id}), skipping")
            return True

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

        # Extract lead ID safely
        try:
            lead_id = response['data'][0]['details']['id']
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"❌ Zoho returned unexpected response structure: {response}")
            # Mark as synced even if we can't get the ID — the record was created
            phone_lead.zoho_synced = True
            phone_lead.save()
            return False

        phone_lead.zoho_lead_id = lead_id
        phone_lead.zoho_synced = True
        phone_lead.save()

        logger.info(f"✅ Created lead in Zoho {zoho_module}: {lead_id}")

        # Create/find Contact and link to lead
        contact_id = _get_or_create_contact_for_phone_lead(phone_lead)
        if contact_id:
            _link_contact_to_lead(zoho_module, lead_id, contact_id, client)

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


# Module → Contact lookup field name
CONTACT_LOOKUP_FIELD = {
    'Deals': 'Client_Contact',
    'Triple_Seal_Apostilles': 'Client_Contact',
    'I_9_Verification': 'Client_Contact',
    'Notary_Services': 'Client_Name',
    'Get_A_Quote_Leads': 'Name_of_Client',
    # Embassy, Apostille, Translation — no contact lookup field in Zoho
}


def _get_or_create_contact_for_phone_lead(phone_lead: 'PhoneCallLead') -> Optional[str]:
    """
    Find or create a Zoho Contact for a phone lead.
    Search order: phone number first, then email.

    Returns:
        Contact ID or None
    """
    from ..zoho_sync import get_access_token, ZOHO_API_DOMAIN

    phone = phone_lead.contact_phone
    email = phone_lead.contact_email
    name = phone_lead.contact_name or 'Phone Lead'

    if not phone and not email:
        logger.info(f"⏭️ No phone or email for phone lead {phone_lead.id}, skipping contact creation")
        return None

    try:
        access_token = get_access_token()
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }

        # 1. Search by phone number
        if phone:
            search_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/search?phone={phone}"
            resp = requests.get(search_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and len(data['data']) > 0:
                    contact_id = data['data'][0]['id']
                    logger.info(f"📇 Found existing contact by phone {phone}: {contact_id}")
                    return contact_id

        # 2. Search by email
        if email:
            search_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/search?email={email}"
            resp = requests.get(search_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and len(data['data']) > 0:
                    contact_id = data['data'][0]['id']
                    logger.info(f"📇 Found existing contact by email {email}: {contact_id}")
                    return contact_id

        # 3. Create new contact
        create_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
        payload = {
            "data": [{
                "Last_Name": name,
                "Email": email or "",
                "Phone": phone or "",
            }]
        }
        resp = requests.post(create_url, headers=headers, json=payload, timeout=30)

        try:
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                item = data['data'][0]
                if 'details' in item:
                    contact_id = item['details']['id']
                    logger.info(f"📇 Created new contact for {name}: {contact_id}")
                    return contact_id
                elif item.get('code') == 'DUPLICATE_DATA' and 'details' in item:
                    contact_id = item['details']['id']
                    logger.info(f"📇 Contact already exists (duplicate): {contact_id}")
                    return contact_id
        except (KeyError, IndexError, TypeError):
            pass

        logger.warning(f"⚠️ Failed to create contact for phone lead {phone_lead.id}: {resp.text}")
        return None

    except Exception as e:
        logger.error(f"❌ Error creating contact for phone lead {phone_lead.id}: {e}", exc_info=True)
        return None


def _link_contact_to_lead(zoho_module: str, lead_id: str, contact_id: str, client: 'ZohoCRMClient'):
    """
    Link a Zoho Contact to a lead record via the module's lookup field.
    Silently skips modules that don't have a contact lookup field.
    """
    lookup_field = CONTACT_LOOKUP_FIELD.get(zoho_module)
    if not lookup_field:
        logger.info(f"⏭️ Module {zoho_module} has no contact lookup field, skipping link")
        return

    link_payload = {lookup_field: {"id": contact_id}}
    response = client.update_record(zoho_module, lead_id, link_payload)

    if response:
        logger.info(f"🔗 Linked contact {contact_id} to {zoho_module} record {lead_id} via {lookup_field}")
    else:
        logger.warning(f"⚠️ Failed to link contact {contact_id} to {zoho_module} record {lead_id}")


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

        logger.info(f"📤 Creating attribution record for lead {zoho_lead_id}")

        # Step 1: Create attribution record in Zoho
        response = client.create_record('Lead_Attribution_Records', zoho_payload)

        if not response or not response.get('data'):
            logger.error(f"❌ Failed to create attribution record: {response}")
            return None

        record_data = response['data'][0]
        if record_data.get('code') != 'SUCCESS' and record_data.get('status') != 'success':
            logger.error(f"❌ Attribution creation failed: {record_data}")
            return None

        attribution_id = record_data.get('details', {}).get('id')
        if not attribution_id:
            logger.error(f"❌ No ID in attribution response: {record_data}")
            return None

        # Step 2: Link attribution to lead by updating the lead record
        # Attribution_Record is a lookup field ON the lead/deal, not on the attribution record
        link_payload = {'Attribution_Record': str(attribution_id)}
        link_response = client.update_record(zoho_module, zoho_lead_id, link_payload)

        if link_response and link_response.get('data'):
            logger.info(f"✅ Linked attribution {attribution_id} to lead {zoho_lead_id}")
        else:
            logger.warning(f"⚠️ Attribution created but failed to link to lead: {link_response}")

        return str(attribution_id)

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
