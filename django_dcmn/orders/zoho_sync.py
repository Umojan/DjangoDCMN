# orders/zoho_sync.py
import requests
import datetime
from django.conf import settings
from django.core.cache import cache
from .models import FbiApostilleOrder, EmbassyLegalizationOrder

ZOHO_API_DOMAIN = 'https://www.zohoapis.com'


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


def sync_fbi_order_to_zoho(order: FbiApostilleOrder):
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
            }
        ]
    }

    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }
        resp = requests.post(f"{ZOHO_API_DOMAIN}/crm/v2/{zoho_module}", headers=headers, json=data)
        resp_data = resp.json()
        print("Create deal:", resp_data)
        try:
            record_id = resp_data['data'][0]['details']['id']
            break
        except Exception as e:
            print("Order creation ERROR:", e)
            if attempt == 1:
                return False

    file_urls = [settings.BASE_URL + fa.file.url for fa in order.file_attachments.all()]
    for url in file_urls:
        file_response = requests.get(url)
        file_response.raise_for_status()
        filename = url.split('/')[-1]
        files = {
            'file': (filename, file_response.content)
        }
        attach_url = f'{ZOHO_API_DOMAIN}/crm/v2/{zoho_module}/{record_id}/Attachments'
        attach_headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}'
        }
        response = requests.post(attach_url, headers=attach_headers, files=files)
        print(f'Attach "{filename}":', response.status_code, response.text)

    order.zoho_synced = True
    order.save(update_fields=['zoho_synced'])
    return True


def sync_embassy_order_to_zoho(order: EmbassyLegalizationOrder):
    zoho_module = 'Embassy_Legalization'
    data = {
        "data": [
            {
                "Name": f"Embassy Legalization #{order.id}",

                "Client_Name": order.name,
                "Email": order.email,
                "Phone": order.phone,
                "Country_of_Legalization": order.country,
                "Address": order.address,
                "Document_Type": order.document_type,

                "Status": "Order Received",
                "Payment_Status": "Not Paid",

                "Client_Comment": order.comments,
            }
        ]
    }

    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }
        resp = requests.post(f"{ZOHO_API_DOMAIN}/crm/v2/{zoho_module}", headers=headers, json=data)
        resp_data = resp.json()
        print("Create embassy deal:", resp_data)
        try:
            record_id = resp_data['data'][0]['details']['id']
            break
        except Exception as e:
            print("Embassy order creation ERROR:", e)
            if attempt == 1:
                return False

    file_urls = [settings.BASE_URL + fa.file.url for fa in order.file_attachments.all()]
    for url in file_urls:
        file_response = requests.get(url)
        file_response.raise_for_status()
        filename = url.split('/')[-1]
        files = {
            'file': (filename, file_response.content)
        }
        attach_url = f'{ZOHO_API_DOMAIN}/crm/v2/{zoho_module}/{record_id}/Attachments'
        attach_headers = {
            'Authorization': f'Zoho-oauthtoken {access_token}'
        }
        response = requests.post(attach_url, headers=attach_headers, files=files)
        print(f'Attach "{filename}":', response.status_code, response.text)

    order.zoho_synced = True
    order.save(update_fields=['zoho_synced'])
    return True
