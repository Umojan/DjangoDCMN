# orders/services/whatconverts.py
"""
WhatConverts integration service.
Handles phone call lead processing, service detection, and duplicate matching.
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
from django.utils.dateparse import parse_datetime
from django.db.models import Q

logger = logging.getLogger(__name__)


# =============================================================================
# SERVICE DETECTION FROM LANDING URL
# =============================================================================

SERVICE_URL_PATTERNS = {
    'apostille': [
        '/apostille',
        '/ssa-letter-apostille-services',
        '/nara-apostille-services',
        '/uscis-apostille-services',
        '/nationwide-apostille-services',
        '/arlington-apostille',
        '/apostille-services-form',
    ],
    'notary': [
        '/online-notary-form',
        '/mobile-notary-services',
    ],
    'i9': [
        '/i-9-verification-form',
        '/i-9',
    ],
    'fbi': [
        '/apostille-fbi',
        '/apostille-fbi-form',
    ],
    'translation': [
        '/translation-services',
        '/translation-form',
        '/translation-languages',
    ],
    'embassy': [
        '/embassy-legalization',
        '/embassy-legalization-form',
    ],
    'marriage': [
        '/triple-seal-marriage',
        '/seal-marriage-form',
    ],
}

# Zoho module mapping
SERVICE_TO_ZOHO_MODULE = {
    'fbi': 'Deals',
    'embassy': 'Embassy_Legalization',
    'translation': 'Translation_Services',
    'apostille': 'Apostille_Services',
    'marriage': 'Triple_Seal_Apostilles',
    'i9': 'I_9_Verification',
    'notary': 'Get_A_Quote_Leads',
    'quote': 'Get_A_Quote_Leads',
}


def detect_service_from_url(landing_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Detect service type from landing URL.

    Returns:
        Tuple of (service_name, zoho_module)
    """
    if not landing_url:
        return None, None

    landing_url_lower = landing_url.lower()

    # Ignore tracking pages
    if '/tracking' in landing_url_lower:
        logger.info(f"Ignoring tracking page: {landing_url}")
        return None, None

    # Check each service pattern
    for service, patterns in SERVICE_URL_PATTERNS.items():
        for pattern in patterns:
            if pattern in landing_url_lower:
                zoho_module = SERVICE_TO_ZOHO_MODULE.get(service)
                logger.info(f"✅ Detected service '{service}' from URL: {landing_url}")
                return service, zoho_module

    logger.info(f"❓ Could not detect service from URL: {landing_url}")
    return None, None


# =============================================================================
# DUPLICATE DETECTION
# =============================================================================

def find_duplicate_phone_lead(phone: str = None, email: str = None, service_type: str = None) -> Optional['PhoneCallLead']:
    """
    Check if a phone call lead already exists with the same contact info AND service type.

    CRITICAL: Only searches within the same service pipeline.
    FBI call → Only matches FBI phone leads
    I-9 call → Only matches I-9 phone leads

    Args:
        phone: Phone number
        email: Email address
        service_type: Service type (fbi, marriage, embassy, etc.)

    Returns:
        Existing PhoneCallLead or None
    """
    from ..models import PhoneCallLead

    if not phone and not email:
        return None

    query = Q()

    if phone:
        # Normalize phone (remove spaces, dashes, parentheses)
        normalized_phone = ''.join(c for c in phone if c.isdigit())
        if normalized_phone:
            query |= Q(contact_phone__icontains=normalized_phone[-10:])  # Match last 10 digits

    if email:
        query |= Q(contact_email__iexact=email)

    # CRITICAL: Filter by service type to avoid cross-service duplicates
    # FBI phone lead should NOT match I-9 phone lead even with same phone
    if service_type:
        query &= Q(detected_service=service_type)
        logger.info(f"🔍 Checking for duplicate phone lead in '{service_type}' pipeline only")

    if query:
        existing = PhoneCallLead.objects.filter(query).order_by('-created_at').first()
        if existing:
            logger.info(f"🔄 Found existing phone lead: {existing.id} (phone={phone}, service={service_type})")
            return existing

    return None


def find_matching_order(phone: str = None, email: str = None, service_type: str = None) -> Optional[Tuple[str, int, object]]:
    """
    Search for matching web form order by phone/email within the same service pipeline.

    CRITICAL: Only searches within the same service type.
    FBI phone call → Only matches FBI orders
    I-9 phone call → Only matches I-9 orders

    Args:
        phone: Phone number from WhatConverts
        email: Email address from WhatConverts
        service_type: Service detected from landing URL (fbi, marriage, embassy, etc.)

    Returns:
        Tuple of (order_type, order_id, order_object) or None
    """
    from ..models import (
        FbiApostilleOrder,
        MarriageOrder,
        EmbassyLegalizationOrder,
        TranslationOrder,
        ApostilleOrder,
        I9VerificationOrder,
        QuoteRequest,
    )

    if not phone and not email:
        return None

    # Normalize phone
    normalized_phone = None
    if phone:
        normalized_phone = ''.join(c for c in phone if c.isdigit())[-10:]

    # List of order models to check
    order_models = [
        ('fbi', FbiApostilleOrder),
        ('marriage', MarriageOrder),
        ('embassy', EmbassyLegalizationOrder),
        ('translation', TranslationOrder),
        ('apostille', ApostilleOrder),
        ('i9', I9VerificationOrder),
        ('quote', QuoteRequest),
    ]

    # If service detected, only check that specific order type
    if service_type:
        order_models = [(order_type, model) for order_type, model in order_models if order_type == service_type]
        logger.info(f"🔍 Searching for orders in '{service_type}' pipeline only")

    for order_type, model in order_models:
        query = Q()

        if normalized_phone:
            query |= Q(phone__icontains=normalized_phone)

        if email:
            query |= Q(email__iexact=email)

        if query:
            order = model.objects.filter(query).order_by('-created_at').first()
            if order:
                logger.info(f"✅ Found matching {order_type} order: {order.id}")
                return (order_type, order.id, order)

    return None


# =============================================================================
# WHATCONVERTS WEBHOOK DATA PARSING
# =============================================================================

def parse_whatconverts_webhook(data: Dict) -> Dict:
    """
    Parse and normalize WhatConverts webhook data.

    Args:
        data: Raw webhook payload from WhatConverts

    Returns:
        Dictionary with normalized phone lead data
    """
    # Extract AI analysis
    lead_analysis = data.get('lead_analysis', {})

    # Parse datetime
    created_at = None
    if data.get('date_created'):
        created_at = parse_datetime(data['date_created'])

    # Detect service from landing URL
    landing_url = data.get('landing_url', '')
    detected_service, zoho_module = detect_service_from_url(landing_url)

    parsed = {
        # Identifiers
        'whatconverts_lead_id': str(data.get('lead_id', '')),

        # Contact info
        'contact_name': data.get('contact_name', ''),
        'contact_email': data.get('contact_email_address') or data.get('email_address'),
        'contact_phone': data.get('contact_phone_number') or data.get('phone_number'),
        'contact_company': data.get('contact_company_name', ''),

        # Call details
        'call_duration': None,  # WhatConverts doesn't send duration for phone calls in this format
        'call_recording_url': None,
        'lead_score': int(data['lead_score']) if data.get('lead_score') is not None else None,
        'lead_status': data.get('lead_status', ''),

        # Service detection
        'detected_service': detected_service or '',
        'landing_url': landing_url,
        'lead_url': data.get('lead_url', ''),

        # Attribution
        'source': data.get('lead_source', ''),
        'medium': data.get('lead_medium', ''),
        'campaign': data.get('lead_campaign', ''),
        'keyword': data.get('lead_keyword', ''),
        'gclid': data.get('gclid', ''),

        # Location
        'city': data.get('city', ''),
        'state': data.get('state', ''),
        'zip_code': data.get('zip', ''),
        'country': data.get('country', ''),

        # Device
        'device_type': data.get('device_type', ''),
        'device_make': data.get('device_make', ''),
        'operating_system': data.get('operating_system', ''),
        'browser': data.get('browser', ''),

        # AI Analysis
        'lead_summary': lead_analysis.get('Lead Summary', ''),
        'sentiment': lead_analysis.get('Sentiment Detection', ''),
        'intent': lead_analysis.get('Intent Detection', ''),
        'spotted_keywords': data.get('spotted_keywords', ''),

        # Raw data
        'raw_webhook_data': data,

        # Sync
        'zoho_module': zoho_module or '',

        # Timestamps
        'whatconverts_created_at': created_at,
    }

    return parsed


# =============================================================================
# MAIN PROCESSING FUNCTION
# =============================================================================

def process_whatconverts_phone_lead(webhook_data: Dict) -> Optional['PhoneCallLead']:
    """
    Main function to process WhatConverts phone call webhook.

    1. Parse webhook data
    2. Check for existing web form orders FIRST (90% probability: clarification call)
    3. If order exists: Skip phone lead creation, add note to order
    4. If no order: Check for duplicate phone leads
    5. Create or update PhoneCallLead

    Args:
        webhook_data: Raw webhook payload from WhatConverts

    Returns:
        PhoneCallLead instance or None (None if matching order exists)
    """
    from ..models import PhoneCallLead

    # Parse webhook data
    parsed = parse_whatconverts_webhook(webhook_data)

    logger.info("=" * 80)
    logger.info(f"📞 Processing WhatConverts Phone Lead: {parsed['whatconverts_lead_id']}")
    logger.info(f"   Contact: {parsed['contact_name']} | {parsed['contact_phone']}")
    logger.info(f"   Service: {parsed['detected_service'] or 'Unknown'}")
    logger.info(f"   Landing: {parsed['landing_url']}")
    logger.info("=" * 80)

    # CRITICAL: Check for existing web form order FIRST
    # 90% probability: call is clarification about existing order
    # If customer wants new service, they'll fill out form (not just call)
    match = find_matching_order(
        phone=parsed['contact_phone'],
        email=parsed['contact_email'],
        service_type=parsed['detected_service']  # Only match within same service pipeline
    )

    if match:
        order_type, order_id, order_obj = match
        logger.info("=" * 80)
        logger.info(f"⏭️ SKIPPING PHONE LEAD CREATION")
        logger.info(f"   Found existing {order_type} order #{order_id}")
        logger.info(f"   Contact: {parsed['contact_name']} | {parsed['contact_phone']}")
        logger.info(f"   90% probability: Clarification call about existing order")
        logger.info(f"   If customer wants NEW service, they'll fill out a form")
        logger.info("=" * 80)

        # Optionally: Add note to order about the call
        # This could be implemented in future if needed:
        # add_call_note_to_order(order_obj, parsed)

        return None  # Don't create phone lead

    # No matching order found - proceed with phone lead creation
    logger.info("✓ No existing order found, proceeding with phone lead creation")

    # Check if this lead already exists in WhatConverts
    existing_lead = PhoneCallLead.objects.filter(
        whatconverts_lead_id=parsed['whatconverts_lead_id']
    ).first()

    if existing_lead:
        logger.info(f"🔄 Updating existing phone lead {existing_lead.id}")
        for key, value in parsed.items():
            setattr(existing_lead, key, value)
        existing_lead.save()
        phone_lead = existing_lead
    else:
        # Check for duplicate by phone/email within same service
        duplicate = find_duplicate_phone_lead(
            phone=parsed['contact_phone'],
            email=parsed['contact_email'],
            service_type=parsed['detected_service']  # CRITICAL: Only same service
        )

        if duplicate:
            logger.info(f"⚠️ Found duplicate phone lead by contact info: {duplicate.id}")
            # Preserve existing Zoho sync data if already synced
            preserve_fields = {'zoho_lead_id', 'zoho_attribution_id', 'zoho_synced', 'zoho_module'}
            for key, value in parsed.items():
                if key in preserve_fields and getattr(duplicate, key, None):
                    continue  # Don't overwrite existing Zoho IDs
                setattr(duplicate, key, value)
            duplicate.save()
            phone_lead = duplicate
        else:
            # Create new phone lead
            phone_lead = PhoneCallLead.objects.create(**parsed)
            logger.info(f"✅ Created new phone lead: {phone_lead.id}")

    return phone_lead


# =============================================================================
# ATTRIBUTION DATA BUILDING FOR ZOHO
# =============================================================================

def build_attribution_from_phone_lead(phone_lead: 'PhoneCallLead') -> Dict:
    """
    Build attribution data dictionary for Zoho Lead Attribution Records.

    Args:
        phone_lead: PhoneCallLead instance

    Returns:
        Dictionary ready for Zoho Attribution payload
    """
    attribution = {
        'source': phone_lead.source,
        'medium': phone_lead.medium or 'phone',
        'campaign': phone_lead.campaign,
        'landing_page': phone_lead.landing_url,
        'lead_url': phone_lead.lead_url,
        'device_type': phone_lead.device_type,
        'browser': phone_lead.browser,
        'city': phone_lead.city,
        'state': phone_lead.state,
        'country': phone_lead.country,
        'gclid': phone_lead.gclid,
        'lead_type': 'phone',  # Always 'phone' for WhatConverts
        'call_duration': phone_lead.call_duration,
        'call_recording_url': phone_lead.call_recording_url,
        'first_visit_at': phone_lead.whatconverts_created_at.isoformat() if phone_lead.whatconverts_created_at else None,
    }

    # Remove None values
    attribution = {k: v for k, v in attribution.items() if v is not None}

    return attribution
