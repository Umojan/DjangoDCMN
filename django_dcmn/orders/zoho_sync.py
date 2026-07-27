# orders/zoho_sync.py
import requests
import datetime
import logging
import os
from copy import deepcopy
from io import BytesIO
from django.conf import settings
from django.core.cache import cache
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder
from .zoho_errors import ZohoPermanentError, ZohoTransientError

logger = logging.getLogger(__name__)

ZOHO_API_DOMAIN = 'https://www.zohoapis.com'
ZOHO_REQUEST_TIMEOUT = (5, 45)
EXTERNAL_ORDER_KEY_FIELD = 'External_Order_Key'

# Module name for Lead Attribution Records
ZOHO_ATTRIBUTION_MODULE = 'Lead_Attribution_Records'

ORDER_TYPE_BY_MODEL = {
    'fbiapostilleorder': 'fbi',
    'embassylegalizationorder': 'embassy',
    'translationorder': 'translation',
    'apostilleorder': 'apostille',
    'marriageorder': 'marriage',
    'i9verificationorder': 'i9',
    'quoterequest': 'quote',
    'prechecksubmission': 'pre-check',
    'fingerprintingsubmission': 'fingerprinting',
    'phonecalllead': 'phone',
}


def get_order_type(order) -> str:
    model_name = order._meta.model_name
    try:
        return ORDER_TYPE_BY_MODEL[model_name]
    except KeyError as exc:
        raise ZohoPermanentError(f'Unsupported order model: {model_name}') from exc


def get_order_external_key(order) -> str:
    return f'dcmn:{get_order_type(order)}:{order.id}'


def get_attribution_external_key(order) -> str:
    return f'dcmn:attribution:{get_order_type(order)}:{order.id}'


def get_or_create_contact_id(name, email, phone):
    if not email:
        return None

    record = {
        'Last_Name': name or 'DCMN Client',
        'Email': email,
        'Phone': phone,
    }
    # Contacts do not carry our order key. Use Zoho's Email duplicate check
    # directly so an ambiguous network retry cannot create another Contact.
    return upsert_record_by_unique_field(
        module_name='Contacts',
        record=record,
        unique_field='Email',
    )


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
    try:
        resp = requests.post(url, params=params, timeout=ZOHO_REQUEST_TIMEOUT)
        resp.raise_for_status()
        token = resp.json()['access_token']
    except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
        raise ZohoTransientError(f'Unable to refresh Zoho access token: {exc}') from exc
    except (ValueError, KeyError) as exc:
        raise ZohoPermanentError('Zoho token response did not contain access_token') from exc

    # Save token to cache
    cache.set("zoho_access_token", token, timeout=2900)  # ~50 min
    return token


def _request(method, url, *, json=None, files=None, params=None):
    """Send a Zoho request, refreshing auth once and classifying retryable errors."""
    for attempt in range(2):
        access_token = get_access_token(force_refresh=(attempt == 1))
        headers = {'Authorization': f'Zoho-oauthtoken {access_token}'}
        if files is None:
            headers['Content-Type'] = 'application/json'

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json,
                files=files,
                params=params,
                timeout=ZOHO_REQUEST_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ZohoTransientError(f'Zoho network error for {method} {url}: {exc}') from exc
        except requests.RequestException as exc:
            raise ZohoTransientError(f'Zoho request error for {method} {url}: {exc}') from exc

        if response.status_code == 401 and attempt == 0:
            continue
        return response

    raise ZohoTransientError(f'Zoho authentication failed for {method} {url}')


def _response_json(response, context):
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        if response.status_code == 429 or response.status_code >= 500:
            raise ZohoTransientError(
                f'{context}: HTTP {response.status_code} with invalid JSON'
            ) from exc
        raise ZohoPermanentError(
            f'{context}: HTTP {response.status_code} with invalid JSON'
        ) from exc


def _first_response_item(data):
    items = data.get('data') or data.get('fields') or []
    return items[0] if items else {}


def _record_id_from_success(data):
    item = _first_response_item(data)
    code = str(item.get('code', '')).upper()
    status = str(item.get('status', '')).lower()
    if code == 'SUCCESS' or status == 'success':
        record_id = item.get('details', {}).get('id')
        if record_id:
            return str(record_id)
    return None


def _raise_zoho_error(response, data, context):
    item = _first_response_item(data)
    code = str(item.get('code', '')).upper()
    message = item.get('message') or response.text[:500]
    detail = f'{context}: HTTP {response.status_code}, code={code or "unknown"}, message={message}'
    if response.status_code == 429 or response.status_code >= 500:
        raise ZohoTransientError(detail)
    raise ZohoPermanentError(detail)


def _resolve_unique_record_id(module_name, unique_field, unique_value):
    """Resolve an already-created record without changing business fields."""
    payload = {
        'data': [{unique_field: unique_value}],
        'duplicate_check_fields': [unique_field],
        'trigger': [],
    }
    response = _request(
        'POST',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/upsert',
        json=payload,
    )
    data = _response_json(response, f'Resolve existing {module_name}')
    record_id = _record_id_from_success(data)
    if record_id:
        return record_id
    _raise_zoho_error(response, data, f'Resolve existing {module_name}')


def upsert_record_by_unique_field(module_name, record, unique_field):
    """Create or update a non-pipeline record using Zoho duplicate checking."""
    payload = {
        'data': [record],
        'duplicate_check_fields': [unique_field],
        'trigger': [],
    }
    response = _request(
        'POST',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/upsert',
        json=payload,
    )
    data = _response_json(response, f'Upsert {module_name}')
    record_id = _record_id_from_success(data)
    if record_id:
        return record_id
    _raise_zoho_error(response, data, f'Upsert {module_name}')


def create_record_idempotently(module_name, record, unique_field, unique_value):
    """
    Insert once using a CRM unique field.

    A retry after an ambiguous timeout receives DUPLICATE_DATA and resolves the
    existing ID with a minimal, trigger-free upsert. Business fields and stages
    are never rolled back by a retry.
    """
    response = _request(
        'POST',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}',
        json={'data': [record]},
    )
    data = _response_json(response, f'Create {module_name}')
    record_id = _record_id_from_success(data)
    if record_id:
        return record_id

    item = _first_response_item(data)
    if str(item.get('code', '')).upper() == 'DUPLICATE_DATA':
        return _resolve_unique_record_id(module_name, unique_field, unique_value)

    _raise_zoho_error(response, data, f'Create {module_name}')


def _existing_attachment_names(module_name, record_id):
    response = _request(
        'GET',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/{record_id}/Attachments',
    )
    if response.status_code == 204:
        return set()
    data = _response_json(response, f'List attachments for {module_name}/{record_id}')
    if response.status_code >= 400:
        _raise_zoho_error(
            response,
            data,
            f'List attachments for {module_name}/{record_id}',
        )
    return {
        item.get('File_Name')
        for item in data.get('data', [])
        if item.get('File_Name')
    }


def sync_order_attachments(order, module_name, record_id):
    """Upload each local file at most once, keyed by its stored filename."""
    existing_names = _existing_attachment_names(module_name, record_id)
    for attachment in order.file_attachments.all():
        filename = os.path.basename(attachment.file.name)
        if filename in existing_names:
            logger.info(
                '[Zoho] Attachment %s already exists on %s/%s, skipping',
                filename,
                module_name,
                record_id,
            )
            continue

        upload_file = None
        close_upload_file = None
        try:
            try:
                attachment.file.open('rb')
                upload_file = attachment.file.file
                close_upload_file = attachment.file.close
            except (FileNotFoundError, OSError):
                # Railway mounts media only on the web service. Celery fetches
                # the same file through the web service when no local volume is
                # available.
                media_url = attachment.file.url
                if not media_url.startswith(('http://', 'https://')):
                    media_url = settings.BASE_URL.rstrip('/') + media_url
                try:
                    file_response = requests.get(
                        media_url,
                        timeout=ZOHO_REQUEST_TIMEOUT,
                    )
                    file_response.raise_for_status()
                except requests.RequestException as exc:
                    raise ZohoTransientError(
                        f'Unable to fetch attachment {filename}: {exc}'
                    ) from exc
                upload_file = BytesIO(file_response.content)
                close_upload_file = upload_file.close

            response = _request(
                'POST',
                f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/{record_id}/Attachments',
                files={'file': (filename, upload_file)},
            )
        finally:
            if close_upload_file:
                close_upload_file()

        data = _response_json(
            response,
            f'Attach {filename} to {module_name}/{record_id}',
        )
        if not _record_id_from_success(data):
            _raise_zoho_error(
                response,
                data,
                f'Attach {filename} to {module_name}/{record_id}',
            )
        existing_names.add(filename)


def _save_remote_id(order, module_name, record_id):
    from .models import ZohoSyncJob

    ZohoSyncJob.objects.update_or_create(
        order_type=get_order_type(order),
        order_id=order.id,
        defaults={
            'zoho_module': module_name,
            'zoho_record_id': record_id,
        },
    )


def sync_order_to_zoho(order, module_name, data_payload, attach_files=True):
    payload = deepcopy(data_payload)
    if not payload.get('data'):
        raise ZohoPermanentError(f'No record data supplied for {module_name}')

    external_key = get_order_external_key(order)
    record = payload['data'][0]
    record[EXTERNAL_ORDER_KEY_FIELD] = external_key

    record_id = create_record_idempotently(
        module_name=module_name,
        record=record,
        unique_field=EXTERNAL_ORDER_KEY_FIELD,
        unique_value=external_key,
    )
    _save_remote_id(order, module_name, record_id)

    if attach_files:
        sync_order_attachments(order, module_name, record_id)

    order.zoho_synced = True
    order.save(update_fields=['zoho_synced'])
    return record_id


# -------- Generic helpers to read/update Zoho records --------
def get_record_by_id(module_name: str, record_id: str, fields: list | None = None):
    """Fetch Zoho CRM record by id. Optionally restrict fields with ?fields=A,B.
    Returns parsed JSON dict, or None when the record does not exist.
    """
    params = {}
    if fields:
        params['fields'] = ','.join(fields)
    response = _request(
        'GET',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}/{record_id}',
        params=params,
    )
    if response.status_code in (204, 404):
        return None
    data = _response_json(response, f'Get {module_name}/{record_id}')
    if response.status_code >= 400:
        _raise_zoho_error(response, data, f'Get {module_name}/{record_id}')
    records = data.get('data') or []
    return records[0] if records else None


def update_record_fields(module_name: str, record_id: str, fields_dict: dict) -> bool:
    """Update Zoho CRM record fields with provided dict.
    Returns True if update succeeded.
    """
    payload = {
        'data': [{'id': record_id, **fields_dict}],
        'trigger': [],
    }
    response = _request(
        'PUT',
        f'{ZOHO_API_DOMAIN}/crm/v2/{module_name}',
        json=payload,
    )
    data = _response_json(response, f'Update {module_name}/{record_id}')
    if _record_id_from_success(data):
        logger.info('[Zoho] Updated %s/%s', module_name, record_id)
        return True
    _raise_zoho_error(response, data, f'Update {module_name}/{record_id}')


# =============================================================================
# LEAD ATTRIBUTION RECORDS
# =============================================================================

def create_attribution_record(
    attribution_data: dict,
    lead_name: str = '',
    *,
    external_key: str,
) -> str | None:
    """
    Create a Lead Attribution Record in Zoho CRM.

    Args:
        attribution_data: Cleaned attribution dict from order.attribution_data
        lead_name: Client name for record naming

    Returns:
        Zoho Record ID (string) or None on failure
    """
    from .services.attribution import build_zoho_attribution_payload

    logger.info('[Zoho Attribution] Building payload for %s', external_key)
    payload = build_zoho_attribution_payload(attribution_data, lead_name)

    if not payload:
        logger.warning('[Zoho Attribution] No payload for %s', external_key)
        return None

    payload['Name'] = f'DCMN Attribution {external_key.removeprefix("dcmn:attribution:")}'
    payload[EXTERNAL_ORDER_KEY_FIELD] = external_key
    return create_record_idempotently(
        module_name=ZOHO_ATTRIBUTION_MODULE,
        record=payload,
        unique_field=EXTERNAL_ORDER_KEY_FIELD,
        unique_value=external_key,
    )


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

    logger.info(
        '[Zoho Attribution] Order %s has attribution data: %s',
        order.id,
        bool(attribution_data),
    )

    # Step 1: Create Attribution Record if we have data
    if attribution_data:
        logger.info(
            '[Zoho Attribution] Creating attribution record for order %s',
            order.id,
        )
        attribution_record_id = create_attribution_record(
            attribution_data,
            lead_name=getattr(order, 'name', ''),
            external_key=get_attribution_external_key(order),
        )
        if attribution_record_id:
            logger.info(
                '[Zoho Attribution] Created record %s',
                attribution_record_id,
            )
        else:
            logger.warning(
                '[Zoho Attribution] No attribution record was created for order %s',
                order.id,
            )

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
                "Stage": "Order Received",
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
                "Client_Address": order.address,
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
                "Client_Address": order.address,
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


def sync_precheck_to_zoho(order):
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)
    zoho_module = 'Get_A_Quote_Leads'

    data = {
        "data": [
            {
                "Name": f"Pre-Check ID{order.id}",
                "Client_Name": order.name,
                "Client_Email": order.email,
                "Client_Phone": order.phone,
                "Document_Type": order.document_type,
                "Country_of_Use": order.destination_country,

                'GET_A_QUOTE_LEADS': 'Pre-Check Document Review',

                "Client_Comments": order.comments or "",

                "Name_of_Client": {"id": contact_id}
            }
        ]
    }

    return sync_order_with_attribution(order, zoho_module, data, attach_files=True)


# =============================================================================
# FINGERPRINTING SYNC (module: FINGERPRINT_SERVICES)
# =============================================================================

# Form service_type choices -> Zoho Fingerprint_Service_Type picklist values
FINGERPRINT_SERVICE_TYPE_MAP = {
    'fbi': 'FBI Live Scan',
    'fd258': 'FD-258 Card',
    'fdle': 'FDLE',
    'atf': 'ATF',
}


def _build_fingerprint_datetime(date_str: str, time_str: str) -> str | None:
    """Combine preferred_date ('YYYY-MM-DD') and preferred_time ('10:30 AM')
    into a timezone-aware ISO 8601 string Zoho accepts. Returns None on failure.

    Appointment times are entered as local wall-clock (Eastern Time / DC), so we
    localize to America/New_York — this keeps the stored instant correct regardless
    of the Zoho org timezone."""
    from datetime import datetime

    if not date_str:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = None

    raw = f"{date_str} {time_str}".strip()
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt.isoformat()
    return None


def sync_fingerprinting_to_zoho(order):
    """Create a lead in the FINGERPRINT_SERVICES pipeline for a fingerprinting form submission.

    Note: this module has no Attribution_Record / Tracking_ID fields, so we use the
    base sync (no attribution linking). Marketing source is folded into Client_Comment.
    """
    zoho_module = 'FINGERPRINT_SERVICES'
    contact_id = get_or_create_contact_id(order.name, order.email, order.phone)

    service_type_value = FINGERPRINT_SERVICE_TYPE_MAP.get(order.service_type)
    appointment_dt = _build_fingerprint_datetime(order.preferred_date, order.preferred_time)

    # Build a readable Client_Comment (also carries data that has no dedicated field)
    comment_parts = []
    if order.service_type:
        comment_parts.append(f"Service requested: {order.get_service_type_display()}")
    comment_parts.append(f"Preferred: {order.preferred_date} {order.preferred_time}")
    comment_parts.append(f"Location: {order.service_location}")
    if order.service_location == 'Mobile' and order.address:
        comment_parts.append(f"Address: {order.address}")
    attr = getattr(order, 'attribution_data', None) or {}
    src, med, camp = attr.get('source'), attr.get('medium'), attr.get('campaign')
    if any([src, med, camp]):
        line = f"Attribution: {src or '-'}/{med or '-'}"
        if camp:
            line += f" · {camp}"
        comment_parts.append(line)
    client_comment = "\n".join(comment_parts)

    record = {
        "Name": f"Fingerprinting ID{order.id} | {order.name}",
        "Email": order.email,
        "Phone": order.phone,
        "Appointment_Type": order.service_location,   # 'Office' | 'Mobile'
        "Fingerprint_Stage": "Client placed the request",
        "Client_Comment": client_comment,
        "Client_Name": {"id": contact_id},
    }
    if service_type_value:
        record["Fingerprint_Service_Type"] = service_type_value
    if appointment_dt:
        record["Appointment_Date_Time"] = appointment_dt
    if order.service_location == 'Mobile' and order.address:
        record["Service_Address_for_mobile"] = order.address

    data = {"data": [record]}
    return sync_order_to_zoho(order, zoho_module, data, attach_files=False)
