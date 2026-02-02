# orders/zoho_sync.py
import requests
import datetime
import logging
from django.conf import settings
from django.core.cache import cache
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder

logger = logging.getLogger(__name__)

ZOHO_API_DOMAIN = 'https://www.zohoapis.com'

# Module name for Lead Attribution Records
ZOHO_ATTRIBUTION_MODULE = 'Lead_Attribution_Records'


def get_or_create_contact_id(name, email, phone):
    access_token = get_access_token()
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Content-Type": "application/json"
    }

    search_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/search?email={email}"
    resp = requests.get(search_url, headers=headers)
    if resp.status_code == 204:
        contact_data = {}
    else:
        try:
            contact_data = resp.json()
        except Exception as e:
            print("❌ Failed to parse Zoho JSON response:", e)
            return None

    if 'data' in contact_data and len(contact_data['data']) > 0:
        return contact_data['data'][0]['id']
    else:
        # Create a new contact
        create_contact_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
        payload = {
            "data": [{
                "Last_Name": name,
                "Email": email,
                "Phone": phone
            }]
        }
        resp = requests.post(create_contact_url, headers=headers, json=payload)
        try:
            created_data = resp.json()
            if 'data' in created_data:
                return created_data['data'][0]['details']['id']
            elif 'data' in created_data and 'details' in created_data['data'][0]:
                return created_data['data'][0]['details']['id']
            elif 'code' in created_data['data'][0] and created_data['data'][0]['code'] == 'DUPLICATE_DATA':
                return created_data['data'][0]['details']['id']
            else:
                print("❌ Error creating contact:", created_data)
                return None
        except Exception as e:
            print("❌ Exception while creating contact:", e)
            return None


def get_access_token(force_refresh=False):
    token = cache.get("zoho_access_token")
    if token and not force_refresh:
        return token

    url = "https://accounts.zoho.com/oauth/v2/token"
    params = {
        "refresh_token": settings.ZOHO_REFRESH_TOKEN,
        "client_id": settings.ZOHO_CLIENT_ID,
        "client_secret": settings.ZOHO_CLIENT_SECRET,
        "grant_type": "refresh_token"
    }
    resp = requests.post(url, params=params)
    resp.raise_for_status()
    token = resp.json()["access_token"]

    # Save token to cache
    cache.set("zoho_access_token", token, timeout=2900)  # ~50 min
    return token


def sync_order_to_zoho(order, module_name, data_payload, attach_files=True):
    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }
        resp = requests.post(f"{ZOHO_API_DOMAIN}/crm/v2/{module_name}", headers=headers, json=data_payload)
        resp_data = resp.json()
        print(f"Create {module_name} deal:", resp_data)
        try:
            record_id = resp_data['data'][0]['details']['id']
            break
        except Exception as e:
            print(f"{module_name} order creation ERROR:", e)
            if attempt == 1:
                return False

    if attach_files:
        file_urls = [settings.BASE_URL + fa.file.url for fa in order.file_attachments.all()]
        for url in file_urls:
            file_response = requests.get(url)
            file_response.raise_for_status()
            filename = url.split('/')[-1]
            files = {
                'file': (filename, file_response.content)
            }
            attach_url = f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/{record_id}/Attachments'
            attach_headers = {
                'Authorization': f'Zoho-oauthtoken {access_token}'
            }
            response = requests.post(attach_url, headers=attach_headers, files=files)
            print(f'Attach "{filename}":', response.status_code, response.text)

    order.zoho_synced = True
    order.save(update_fields=['zoho_synced'])
    return True


# -------- Generic helpers to read/update Zoho records --------
def get_record_by_id(module_name: str, record_id: str, fields: list | None = None):
    """Fetch Zoho CRM record by id. Optionally restrict fields with ?fields=A,B.
    Returns parsed JSON dict or None on error.
    """
    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
        }
        params = {}
        if fields:
            params["fields"] = ",".join(fields)
        url = f"{ZOHO_API_DOMAIN}/crm/v2/{module_name}/{record_id}"
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code == 401 and attempt == 0:
            # token expired, retry once
            continue
        try:
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                return data['data'][0]
        except Exception:
            return None
    return None


def update_record_fields(module_name: str, record_id: str, fields_dict: dict) -> bool:
    """Update Zoho CRM record fields with provided dict.
    Returns True if update succeeded.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    payload = {"data": [{"id": record_id, **fields_dict}]}
    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        }
        url = f"{ZOHO_API_DOMAIN}/crm/v2/{module_name}"
        
        logger.info(f"[Zoho] PUT {url} payload={payload}")
        resp = requests.put(url, headers=headers, json=payload)
        logger.info(f"[Zoho] Response status={resp.status_code}, body={resp.text}")
        
        if resp.status_code == 401 and attempt == 0:
            # token expired, retry once
            continue
            
        try:
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                item = data['data'][0]
                # Check both 'code' and 'status' fields for success
                code = item.get('code', '').upper()
                status = item.get('status', '').lower()
                if code == 'SUCCESS' or status == 'success':
                    logger.info(f"[Zoho] ✅ Successfully updated {module_name}/{record_id}")
                    return True
                else:
                    logger.warning(f"[Zoho] Update failed: code={code}, status={status}, message={item.get('message')}")
        except Exception as e:
            logger.exception(f"[Zoho] Exception parsing response: {e}")
            
    return False


# =============================================================================
# LEAD ATTRIBUTION RECORDS
# =============================================================================

def create_attribution_record(attribution_data: dict, lead_name: str = '') -> str | None:
    """
    Create a Lead Attribution Record in Zoho CRM.

    Args:
        attribution_data: Cleaned attribution dict from order.attribution_data
        lead_name: Client name for record naming

    Returns:
        Zoho Record ID (string) or None on failure
    """
    from .services.attribution import build_zoho_attribution_payload

    print(f"\n🔍 [DEBUG] create_attribution_record called")
    print(f"🔍 [DEBUG] Input attribution_data: {attribution_data}")
    print(f"🔍 [DEBUG] Lead name: {lead_name}")

    logger.info(f"[Zoho Attribution] Building payload from: {attribution_data}")
    payload = build_zoho_attribution_payload(attribution_data, lead_name)

    if not payload:
        print(f"❌ [DEBUG] build_zoho_attribution_payload returned None!")
        logger.warning("[Zoho Attribution] build_zoho_attribution_payload returned None!")
        return None

    print(f"✅ [DEBUG] Payload built: {payload}")
    logger.info(f"[Zoho Attribution] Payload built: {payload}")
    zoho_payload = {"data": [payload]}

    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }

        url = f"{ZOHO_API_DOMAIN}/crm/v2/{ZOHO_ATTRIBUTION_MODULE}"
        logger.info(f"[Zoho] Creating Attribution Record: {payload.get('Name')}")

        try:
            print(f"\n🔍 [DEBUG] POST {url}")
            print(f"🔍 [DEBUG] Request body: {zoho_payload}")
            logger.info(f"[Zoho Attribution] POST {url}")
            logger.info(f"[Zoho Attribution] Request body: {zoho_payload}")
            resp = requests.post(url, headers=headers, json=zoho_payload)
            print(f"🔍 [DEBUG] Response status: {resp.status_code}")
            print(f"🔍 [DEBUG] Response body: {resp.text}")
            logger.info(f"[Zoho Attribution] Response status: {resp.status_code}")
            logger.info(f"[Zoho Attribution] Response body: {resp.text}")
            resp_data = resp.json()

            if resp.status_code == 401 and attempt == 0:
                logger.warning("[Zoho] Token expired, refreshing...")
                continue

            # Extract record ID from response
            if 'data' in resp_data and len(resp_data['data']) > 0:
                item = resp_data['data'][0]
                if item.get('code') == 'SUCCESS' or item.get('status') == 'success':
                    record_id = item.get('details', {}).get('id')
                    if record_id:
                        print(f"✅ [DEBUG] Attribution Record created: {record_id}")
                        logger.info(f"[Zoho] ✅ Created Attribution Record: {record_id}")
                        return str(record_id)
                else:
                    print(f"❌ [DEBUG] Attribution creation failed: {item}")
                    logger.warning(f"[Zoho] Attribution creation failed: {item}")
            else:
                print(f"❌ [DEBUG] Unexpected response: {resp_data}")
                logger.warning(f"[Zoho] Unexpected response: {resp_data}")

        except Exception as e:
            logger.exception(f"[Zoho] Exception creating Attribution Record: {e}")
            if attempt == 1:
                return None

    return None


def sync_order_with_attribution(order, module_name: str, data_payload: dict, attach_files: bool = True) -> bool:
    """
    Sync order to Zoho with Attribution Record lookup.

    1. Creates Lead_Attribution_Record if order has attribution_data
    2. Adds Attribution_Record lookup to order payload
    3. Creates order in the specified module

    Args:
        order: Order model instance with attribution_data
        module_name: Zoho module name (e.g., 'Apostille_Services')
        data_payload: Prepared Zoho payload dict with "data" key
        attach_files: Whether to attach files

    Returns:
        True on success, False on failure
    """
    attribution_data = getattr(order, 'attribution_data', None)
    attribution_record_id = None

    # DEBUG: Print to console (always visible)
    print(f"\n🔍 [DEBUG] sync_order_with_attribution called for order {order.id}")
    print(f"🔍 [DEBUG] Has attribution_data: {bool(attribution_data)}")
    if attribution_data:
        print(f"🔍 [DEBUG] Attribution data: {attribution_data}")

    logger.info(f"[Zoho Attribution] Order {order.id} has attribution_data: {bool(attribution_data)}")
    if attribution_data:
        logger.info(f"[Zoho Attribution] Data: {attribution_data}")

    # Step 1: Create Attribution Record if we have data
    if attribution_data:
        print(f"🔍 [DEBUG] Creating attribution record...")
        logger.info(f"[Zoho Attribution] Creating attribution record for order {order.id}...")
        attribution_record_id = create_attribution_record(
            attribution_data,
            lead_name=getattr(order, 'name', '')
        )
        if attribution_record_id:
            print(f"✅ [DEBUG] Attribution record created: {attribution_record_id}")
            logger.info(f"[Zoho Attribution] ✅ Created record: {attribution_record_id}")
        else:
            print(f"❌ [DEBUG] Failed to create attribution record!")
            logger.warning(f"[Zoho Attribution] ❌ Failed to create attribution record")

    # Step 2: Add Attribution lookup to order payload
    if attribution_record_id and 'data' in data_payload and len(data_payload['data']) > 0:
        data_payload['data'][0]['Attribution_Record'] = attribution_record_id
        logger.info(f"[Zoho] Linking order to Attribution Record: {attribution_record_id}")

    # Step 3: Create order (using existing function)
    return sync_order_to_zoho(order, module_name, data_payload, attach_files)


# =============================================================================
# ORDER SYNC FUNCTIONS (with attribution support)
# =============================================================================

def sync_fbi_order_to_zoho(order: FbiApostilleOrder, tracking_id: str | None = None):
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)
    zoho_module = 'Deals'
    data = {
        "data": [
            {
                "Deal_Name": f"FBI {order.package.label} ID{order.id}",
                "Order_ID": order.id,
                "Name1": order.name,
                "Email_1": order.email,
                "Phone": order.phone,
                "Country_of_Use": order.country_name,
                "Client_Comment": order.comments,
                "Address": order.address,
                "Package": order.package.label,
                "Certificate": str(order.count),
                "Shipping_speed": order.shipping_option.label,
                "Amount": float(order.total_price),
                "Status": "Order Received",
                "Payment_Status": "Fully Paid" if order.is_paid else "Not Paid",
                "Submission_Date": order.created_at.date().isoformat(),
                "Client_Contact": {"id": contact_id},
                **({"Tracking_ID": tracking_id} if tracking_id else {}),
            }
        ]
    }
    # Use attribution-aware sync
    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


def sync_embassy_order_to_zoho(order: EmbassyLegalizationOrder, tracking_id: str | None = None):
    zoho_module = 'Embassy_Legalization'
    data = {
        "data": [
            {
                "Name": f"Embassy ID{order.id}",

                "Client_Name": order.name,
                "Email": order.email,
                "Phone": order.phone,
                "Country_of_Legalization": order.country,
                "Address": order.address,
                "Document_Type": order.document_type,

                "Status": "Order Received",
                "Payment_Status": "Not Paid",

                "Client_Comment": order.comments,
                **({"Tracking_ID": tracking_id} if tracking_id else {}),
            }
        ]
    }
    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


def sync_translation_order_to_zoho(order: TranslationOrder, tracking_id: str | None = None):
    zoho_module = 'Translation_Services'
    data = {
        "data": [
            {
                "Name": f"Translation {order.name} ID{order.id}",
                "Client_Name1": order.name,
                "Email": order.email,
                "Phone": order.phone,
                "Address": order.address,
                "Languages": order.languages,
                "Client_Comments": order.comments,
                "Translation_Status": "Client Placed Request",
                **({"Tracking_ID": tracking_id} if tracking_id else {}),
            }
        ]
    }
    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


def sync_apostille_order_to_zoho(order: ApostilleOrder, tracking_id: str | None = None):
    zoho_module = 'Apostille_Services'

    data = {
        "data": [
            {
                "Name": f"Apostille Order ID{order.id}",
                "Client_Name": order.name,
                "Email": order.email,
                "Phone_Number": order.phone,
                "Address": order.address or "- Office Visit -",
                "Country_of_Use": order.country,
                "Document_Type": order.type,
                "Client_Comments": order.comments or "",
                "Status": "Client placed the request",
                "Process_Stage": "Submission Received",
                **({"Tracking_ID": tracking_id} if tracking_id else {}),
            }
        ]
    }
    return sync_order_with_attribution(order, zoho_module, data, attach_files=False)


def sync_marriage_order_to_zoho(order, tracking_id: str | None = None):
    """Sync Marriage/Triple Seal order to Zoho. tracking_id accepted but not used."""
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)
    zoho_module = 'Triple_Seal_Apostilles'

    marriage_info = "- File Uploaded -"
    if not order.file_attachments.exists():
        marriage_info = (
            f"Husband: {order.husband_full_name}\n"
            f"Wife: {order.wife_full_name}\n"
            f"Date of marriage: {order.marriage_date}\n"
            f"Certificate Number: {order.marriage_number}\n"
            f"Country of Use: {order.country}"
        )

    data = {
        "data": [
            {
                "Name": f"Triple Seal ID{order.id}",
                "Client_Name": order.name,
                "Client_Email": order.email,
                "Client_Phone": order.phone,
                "Address": order.address,
                "Type_of_Legalization": "Triple Seal",
                "Stage": "Order Received",
                "Payment_Status": "Deposit",
                "Amount_Paid": str(order.total_price),

                "Marriage_Info": marriage_info,

                "Client_Notes_Comments": order.comments or "",
                "Client_Contact": {"id": contact_id},
            }
        ]
    }

    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


def sync_i9_order_to_zoho(order, tracking_id: str | None = None):
    """Sync I-9 Verification order to Zoho. tracking_id accepted but not used."""
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)
    zoho_module = 'I_9_Verification'

    data = {
        "data": [
            {
                "Name": f"I9 Verification ID{order.id}",
                "Client_Name": order.name,
                "Client_Email": order.email,
                "Client_Phone": order.phone,
                "Address": order.address,
                "Form_Date_Time": f'{order.appointment_date} - {order.appointment_time}',
                "Stage": "Order Received",
                "Client_Comments": order.comments or "",
                "Client_Contact": {"id": contact_id},
            }
        ]
    }

    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


def sync_quote_request_to_zoho(order):
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)
    zoho_module = 'Get_A_Quote_Leads'

    data = {
        "data": [
            {
                "Name": f"Quote ID{order.id}",
                "Client_Name": order.name,
                "Client_Email": order.email,
                "Client_Phone": order.phone,
                "Number_Of_Documents": str(order.number),
                "Client_Address_Location": order.address,
                "Date_Time": order.appointment_date + ' - ' + order.appointment_time,

                'GET_A_QUOTE_LEADS': order.services,

                "Client_Comments": order.comments or "",

                "Name_of_Client": {"id": contact_id}
            }
        ]
    }

    return sync_order_with_attribution(order, zoho_module, data, attach_files=False)
