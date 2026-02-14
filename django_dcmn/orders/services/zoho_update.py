# orders/services/zoho_update.py
"""
Update existing Zoho CRM record with full form data.

Used when a web form matches an existing phone lead (zoho_synced=True).
Instead of creating a new Zoho record, updates the phone lead's record
with all form-specific data (comments, package, shipping, address, etc.)
"""

import logging
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)


def update_matched_zoho_record(order, order_type: str, tracking_id: str = None) -> bool:
    """
    Find matched phone lead for this order and update its Zoho record
    with full form data + files.

    Args:
        order: Order model instance (zoho_synced must be True)
        order_type: Service type (fbi, embassy, etc.)
        tracking_id: Optional tracking ID to write to Zoho

    Returns:
        True if updated successfully
    """
    from ..models import PhoneCallLead
    from ..zoho_client import ZohoCRMClient

    # Find the matched phone lead
    phone_lead = PhoneCallLead.objects.filter(
        matched_order_type=order_type,
        matched_order_id=order.id,
        zoho_lead_id__gt='',
    ).first()

    if not phone_lead:
        logger.warning(
            f"[ZohoUpdate] No matched phone lead found for {order_type} order {order.id}. "
            f"zoho_synced=True but no phone lead — skipping."
        )
        return False

    zoho_module = phone_lead.zoho_module
    zoho_lead_id = phone_lead.zoho_lead_id

    if not zoho_module or not zoho_lead_id:
        logger.warning(f"[ZohoUpdate] Phone lead {phone_lead.id} missing module/id")
        return False

    # Build update payload with full form data (same fields as CREATE in zoho_sync.py)
    update_payload = _build_full_update_payload(order, order_type, tracking_id)

    if not update_payload:
        logger.warning(f"[ZohoUpdate] Empty payload for {order_type} order {order.id}")
        return False

    logger.info(f"[ZohoUpdate] Updating {zoho_module}/{zoho_lead_id} with form data for {order_type} order {order.id}")
    logger.info(f"[ZohoUpdate] Payload: {update_payload}")

    try:
        client = ZohoCRMClient()
        response = client.update_record(zoho_module, zoho_lead_id, update_payload)

        if response and response.get('data'):
            logger.info(f"[ZohoUpdate] ✅ Updated {zoho_module}/{zoho_lead_id} with full form data")

            # Attach files to existing Zoho record
            _attach_files_to_record(order, zoho_module, zoho_lead_id)

            return True

        logger.error(f"[ZohoUpdate] ❌ Failed to update: {response}")
        return False

    except Exception as e:
        logger.error(f"[ZohoUpdate] ❌ Error: {e}", exc_info=True)
        return False


def _build_full_update_payload(order, order_type: str, tracking_id: str = None) -> dict:
    """
    Build full update payload matching what zoho_sync.py CREATE functions send.
    Excludes stage field — that's handled by phone_lead_matcher.py conditional logic.
    """
    payload = {}

    if order_type == 'fbi':
        payload = {
            'Deal_Name': f"FBI {order.package.label} ID{order.id}",
            'Order_ID': order.id,
            'Name1': order.name,
            'Email_1': order.email,
            'Phone': order.phone,
            'Country_of_Use': order.country_name,
            'Client_Comment': order.comments,
            'Address': order.address,
            'Package': order.package.label,
            'Certificate': str(order.count),
            'Shipping_speed': order.shipping_option.label,
            'Amount': float(order.total_price),
            'Payment_Status': 'Fully Paid' if order.is_paid else 'Not Paid',
            'Submission_Date': order.created_at.date().isoformat(),
        }

    elif order_type == 'embassy':
        payload = {
            'Name': f"Embassy ID{order.id}",
            'Client_Name': order.name,
            'Email': order.email,
            'Phone': order.phone,
            'Country_of_Legalization': order.country,
            'Address': order.address,
            'Document_Type': order.document_type,
            'Payment_Status': 'Not Paid',
            'Client_Comment': order.comments,
        }

    elif order_type == 'translation':
        payload = {
            'Name': f"Translation {order.name} ID{order.id}",
            'Client_Name1': order.name,
            'Email': order.email,
            'Phone': order.phone,
            'Address': order.address,
            'Languages': order.languages,
            'Client_Comments': order.comments,
        }

    elif order_type == 'apostille':
        payload = {
            'Name': f"Apostille Order ID{order.id}",
            'Client_Name': order.name,
            'Email': order.email,
            'Phone_Number': order.phone,
            'Address': order.address or '- Office Visit -',
            'Country_of_Use': order.country,
            'Document_Type': order.type,
            'Client_Comments': order.comments or '',
            'Process_Stage': 'Submission Received',
        }

    elif order_type == 'marriage':
        marriage_info = '- File Uploaded -'
        if not order.file_attachments.exists():
            marriage_info = (
                f"Husband: {order.husband_full_name}\n"
                f"Wife: {order.wife_full_name}\n"
                f"Date of marriage: {order.marriage_date}\n"
                f"Certificate Number: {order.marriage_number}\n"
                f"Country of Use: {order.country}"
            )
        payload = {
            'Name': f"Triple Seal ID{order.id}",
            'Client_Name': order.name,
            'Client_Email': order.email,
            'Client_Phone': order.phone,
            'Address': order.address,
            'Type_of_Legalization': 'Triple Seal',
            'Payment_Status': 'Deposit',
            'Amount_Paid': str(order.total_price),
            'Marriage_Info': marriage_info,
            'Client_Notes_Comments': order.comments or '',
        }

    elif order_type == 'I-9' or order_type == 'i9':
        payload = {
            'Name': f"I9 Verification ID{order.id}",
            'Client_Name': order.name,
            'Client_Email': order.email,
            'Client_Phone': order.phone,
            'Address': order.address,
            'Form_Date_Time': f'{order.appointment_date} - {order.appointment_time}',
            'Client_Comments': order.comments or '',
        }

    elif order_type == 'quote':
        payload = {
            'Name': f"Quote ID{order.id}",
            'Client_Name': order.name,
            'Client_Email': order.email,
            'Client_Phone': order.phone,
            'Number_Of_Documents': str(order.number),
            'Client_Address_Location': order.address,
            'Date_Time': order.appointment_date + ' - ' + order.appointment_time,
            'GET_A_QUOTE_LEADS': order.services,
            'Client_Comments': order.comments or '',
        }

    # Add tracking ID if provided
    if tracking_id:
        payload['Tracking_ID'] = tracking_id

    # Remove None values
    payload = {k: v for k, v in payload.items() if v is not None}

    return payload


def _attach_files_to_record(order, zoho_module: str, zoho_record_id: str):
    """Attach order files to existing Zoho record."""
    import requests
    from ..zoho_sync import get_access_token, ZOHO_API_DOMAIN

    # QuoteRequest has no file_attachments GenericRelation
    if not hasattr(order, 'file_attachments'):
        return

    try:
        file_urls = [settings.BASE_URL + fa.file.url for fa in order.file_attachments.all()]
        if not file_urls:
            return

        access_token = get_access_token()

        for url in file_urls:
            try:
                file_response = requests.get(url)
                file_response.raise_for_status()
                filename = url.split('/')[-1]

                attach_url = f'{ZOHO_API_DOMAIN}/crm/v2/{zoho_module}/{zoho_record_id}/Attachments'
                attach_headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
                files = {'file': (filename, file_response.content)}

                response = requests.post(attach_url, headers=attach_headers, files=files)
                logger.info(f'[ZohoUpdate] Attached "{filename}": {response.status_code}')
            except Exception as e:
                logger.error(f'[ZohoUpdate] Failed to attach file {url}: {e}')

    except Exception as e:
        logger.error(f'[ZohoUpdate] Error attaching files: {e}', exc_info=True)
