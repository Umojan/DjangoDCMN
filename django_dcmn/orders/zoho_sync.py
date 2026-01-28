# orders/zoho_sync.py
import requests
import datetime
from django.conf import settings
from django.core.cache import cache
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder

ZOHO_API_DOMAIN = 'https://www.zohoapis.com'


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
    return sync_order_to_zoho(order, zoho_module, data, attach_files=True)


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
    return sync_order_to_zoho(order, zoho_module, data, attach_files=True)


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
    return sync_order_to_zoho(order, zoho_module, data, attach_files=True)


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
    return sync_order_to_zoho(order, zoho_module, data, attach_files=False)


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

    return sync_order_to_zoho(order, zoho_module, data, attach_files=True)


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

    return sync_order_to_zoho(order, zoho_module, data, attach_files=True)


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

    return sync_order_to_zoho(order, zoho_module, data, attach_files=False)
