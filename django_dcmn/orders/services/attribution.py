# orders/services/attribution.py
"""
Lead Attribution service - handles marketing data from frontend and Zoho sync.

Flow:
1. Frontend sends attribution data with form submission
2. Django extracts and cleans the data
3. Data is saved to order.attribution_data (JSONField)
4. On Zoho sync: create Lead_Attribution_Records, get ID, link to order
"""

import logging
import json
from typing import Any
from datetime import datetime

logger = logging.getLogger(__name__)


# =============================================================================
# FIELD MAPPING: Frontend → Zoho API
# =============================================================================

ATTRIBUTION_FIELD_MAP = {
    # Traffic Source
    'source': 'Source',
    'medium': 'Medium',
    'campaign': 'Campaign',
    'utm_source': 'Source',  # fallback
    'utm_medium': 'Medium',  # fallback
    'utm_campaign': 'Campaign',  # fallback
    'utm_content': 'UTM_Content',
    'utm_term': 'UTM_Term',

    # Click IDs
    'gclid': 'GCLID',
    'fbclid': 'FBCLID',
    'msclkid': 'MSCLKID',

    # Landing Info
    'landing_page': 'Landing_Page',
    'lead_url': 'Lead_URL',
    'referrer_domain': 'Referrer_Domain',

    # Device
    'device_type': 'Device_Type',

    # Engagement
    'pages_viewed': 'Pages_Viewed',
    'visit_count': 'Visit_Count',
    'first_visit_at': 'First_Visit_At',

    # Location
    'city': 'City',
    'state': 'State',
    'country': 'Country',

    # Lead info
    'lead_type': 'Lead_Type',

    # Call data (for WhatConverts integration)
    'call_duration': 'Call_Duration',
    'call_recording_url': 'Call_Recording_URL',
}

# Zoho Picklist values
DEVICE_TYPE_OPTIONS = {
    'mobile': 'Mobile',
    'tablet': 'Tablet',
    'desktop': 'Desktop',
}

LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'chat': 'Chat',
}


# =============================================================================
# DATA EXTRACTION & CLEANING
# =============================================================================

def extract_attribution_from_request(request) -> dict | None:
    """
    Extract attribution data from Django/DRF request.
    Supports both JSON body and FormData.

    Args:
        request: Django HttpRequest or DRF Request

    Returns:
        Cleaned attribution dict or None
    """
    attribution_raw = None

    # Try DRF request.data first (works for both JSON and FormData)
    if hasattr(request, 'data'):
        attribution_raw = request.data.get('attribution')

    # Try Django POST (FormData) - only if DRF data didn't have it
    if not attribution_raw and hasattr(request, 'POST'):
        attribution_raw = request.POST.get('attribution')

    # NOTE: We intentionally don't try request.body here because:
    # 1. DRF's request.data already handles JSON bodies
    # 2. Reading body after POST causes RawPostDataException

    if not attribution_raw:
        return None

    # Parse if string (FormData sends JSON as string)
    if isinstance(attribution_raw, str):
        try:
            attribution_raw = json.loads(attribution_raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Failed to parse attribution JSON: {attribution_raw[:100]}")
            return None

    return clean_attribution_data(attribution_raw)


def clean_attribution_data(raw_data: dict | None) -> dict | None:
    """
    Clean and normalize attribution data.
    Removes null/empty values, normalizes types.

    Args:
        raw_data: Raw attribution dict from frontend

    Returns:
        Cleaned dict or None if empty
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    cleaned = {}

    for key, value in raw_data.items():
        # Skip null, None, empty strings, 'null' string
        if value is None or value == '' or value == 'null' or value == 'undefined':
            continue

        # Normalize device_type to lowercase
        if key == 'device_type' and isinstance(value, str):
            value = value.lower()

        # Convert numeric fields (Zoho expects Number type)
        if key in ('pages_viewed', 'visit_count', 'call_duration'):
            try:
                value = int(value)
            except (ValueError, TypeError):
                continue

        # Normalize datetime fields (Zoho expects DateTime without milliseconds)
        # Frontend sends: "2024-01-15T10:30:00.636Z"
        # Zoho needs: "2024-01-15T10:30:00" or "2024-01-15T10:30:00Z"
        if key == 'first_visit_at' and isinstance(value, str):
            # Remove milliseconds: .636 or .636Z
            import re
            value = re.sub(r'\.\d+', '', value)
            # Zoho accepts: "2024-01-15T10:30:00Z", "2024-01-15T10:30:00", "2024-01-15"

        # Truncate very long strings (protection)
        if isinstance(value, str) and len(value) > 2000:
            value = value[:2000]

        cleaned[key] = value

    return cleaned if cleaned else None


# =============================================================================
# ZOHO PAYLOAD BUILDING
# =============================================================================

def build_zoho_attribution_payload(attribution_data: dict, lead_name: str = '') -> dict | None:
    """
    Transform attribution data into Zoho API format for Lead_Attribution_Records.

    Args:
        attribution_data: Cleaned attribution dict
        lead_name: Client name for record naming

    Returns:
        Dict ready for Zoho API or None if no data
    """
    if not attribution_data:
        return None

    payload = {}

    # Map fields with priority handling (source > utm_source)
    mapped_zoho_fields = set()

    for frontend_key, zoho_field in ATTRIBUTION_FIELD_MAP.items():
        # Skip if we already set this Zoho field (priority handling)
        if zoho_field in mapped_zoho_fields:
            continue

        value = attribution_data.get(frontend_key)
        if value is None:
            continue

        # Transform Picklist values
        if frontend_key == 'device_type':
            value = DEVICE_TYPE_OPTIONS.get(str(value).lower(), 'Desktop')
        elif frontend_key == 'lead_type':
            value = LEAD_TYPE_OPTIONS.get(str(value).lower(), 'Form')

        payload[zoho_field] = value
        mapped_zoho_fields.add(zoho_field)

    if not payload:
        return None

    # Generate unique Name for Zoho record (required field)
    source = attribution_data.get('source', 'direct')
    medium = attribution_data.get('medium', 'none')
    date_str = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Truncate lead_name to 20 chars
    name_part = lead_name[:20] if lead_name else 'Lead'
    payload['Name'] = f"{name_part} | {source}/{medium} | {date_str}"

    # Also set Lead_URL if landing_page exists but lead_url doesn't
    if 'Landing_Page' in payload and 'Lead_URL' not in payload:
        payload['Lead_URL'] = payload['Landing_Page']

    return payload


# =============================================================================
# GEO ENRICHMENT (Optional)
# =============================================================================

def get_client_ip(request) -> str:
    """Get real client IP considering proxies/load balancers."""
    # Check common proxy headers
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        # Take first IP in chain
        return x_forwarded_for.split(',')[0].strip()

    x_real_ip = request.META.get('HTTP_X_REAL_IP')
    if x_real_ip:
        return x_real_ip.strip()

    return request.META.get('REMOTE_ADDR', '')


def enrich_with_geo(attribution_data: dict, request) -> dict:
    """
    Enrich attribution with geo data from IP.

    For now, just returns data as-is.

    Example implementation:
    ```
    ip = get_client_ip(request)
    if ip and not ip.startswith(('127.', '192.168.', '10.')):
        try:
            resp = requests.get(f'https://ipinfo.io/{ip}/json?token=YOUR_TOKEN', timeout=2)
            geo = resp.json()
            attribution_data['city'] = geo.get('city')
            attribution_data['state'] = geo.get('region')
            attribution_data['country'] = geo.get('country')
        except:
            pass
    ```
    """
    # Placeholder - implement when GeoIP service is set up
    return attribution_data


# =============================================================================
# MAIN PROCESSING FUNCTION
# =============================================================================

def process_attribution(request, order) -> dict | None:
    """
    Main function to process attribution for an order.

    1. Extracts attribution from request
    2. Cleans and validates
    3. Optionally enriches with geo
    4. Saves to order.attribution_data

    Args:
        request: Django/DRF request
        order: Order model instance

    Returns:
        Processed attribution dict or None
    """
    attribution = extract_attribution_from_request(request)

    if not attribution:
        logger.debug(f"No attribution data for order {order.id}")
        return None

    # Enrich with geo (if configured)
    attribution = enrich_with_geo(attribution, request)

    # Save to order
    try:
        order.attribution_data = attribution
        order.save(update_fields=['attribution_data'])
        logger.info(f"Saved attribution for order {order.id}: source={attribution.get('source')}, medium={attribution.get('medium')}")
    except Exception as e:
        logger.exception(f"Failed to save attribution for order {order.id}: {e}")

    return attribution
