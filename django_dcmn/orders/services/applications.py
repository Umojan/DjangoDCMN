"""
Business Account / Partner application service layer.

Shared by the two public endpoints (/api/business-accounts/apply/,
/api/partners/apply/): payload normalisation, human-readable questionnaire
rendering (used for the Zoho Lead description AND the manager email),
manager notifications and Cloudflare Turnstile verification.
"""

import logging
import requests
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives

from ..models import Application

logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

# Form field names differ per program; both map onto the same model columns.
PROGRAM_FIELD_MAP = {
    Application.PROGRAM_BUSINESS_ACCOUNT: {
        'organization': 'organization',
        'location': 'city_state',
        'org_type': 'org_type',
        'volume': 'order_frequency',
    },
    Application.PROGRAM_PARTNER: {
        'organization': 'company',
        'location': 'state_country',
        'org_type': 'business_type',
        'volume': 'monthly_volume',
    },
}

# Labels shown to managers (Zoho Description / email) per program.
PROGRAM_LABELS = {
    Application.PROGRAM_BUSINESS_ACCOUNT: {
        'title': 'Business Account Application',
        'marker': '[BUSINESS ACCOUNT APPLICATION]',
        'organization': 'Organization',
        'location': 'City / State',
        'org_type': 'Organization type',
        'volume': 'Order frequency',
    },
    Application.PROGRAM_PARTNER: {
        'title': 'Partner Application',
        'marker': '[PARTNER APPLICATION]',
        'organization': 'Company',
        'location': 'State / Country',
        'org_type': 'Business type',
        'volume': 'Monthly volume',
    },
}

SERVICE_LABELS = {
    'federal_apostille': 'Federal apostille (U.S. Dept. of State)',
    'state_apostille': 'State apostille',
    'embassy': 'Embassy legalization',
    'fbi': 'FBI background check apostille',
    'livescan': 'Live scan fingerprinting',
    'notary': 'Notary',
    'translation': 'Certified translation',
    'i9': 'I-9 verification',
    'other': 'Other',
}


def service_labels(keys) -> list[str]:
    return [SERVICE_LABELS.get(k, str(k)) for k in (keys or [])]


# =============================================================================
# QUESTIONNAIRE RENDERING (shared by Zoho Description and manager email)
# =============================================================================

def _attribution_lines(app: Application) -> list[str]:
    attr = app.attribution_data or {}
    if not attr:
        return []
    pairs = [
        ('Source', attr.get('source') or attr.get('utm_source')),
        ('Medium', attr.get('medium') or attr.get('utm_medium')),
        ('Campaign', attr.get('campaign') or attr.get('utm_campaign')),
        ('UTM content', attr.get('utm_content')),
        ('UTM term', attr.get('utm_term')),
        ('GCLID', attr.get('gclid')),
        ('FBCLID', attr.get('fbclid')),
        ('MSCLKID', attr.get('msclkid')),
        ('Landing page', attr.get('landing_page')),
        ('Referrer', attr.get('referrer_domain') or attr.get('referrer')),
        ('Device', attr.get('device_type')),
        ('Visits', attr.get('visit_count')),
    ]
    lines = [f"  {label}: {value}" for label, value in pairs if value not in (None, '', 'null')]
    return ['Attribution:'] + lines if lines else []


def build_questionnaire(app: Application, include_marker: bool = True) -> str:
    """Plain-text summary of the application, one field per line."""
    labels = PROGRAM_LABELS[app.program]
    lines = []
    if include_marker:
        lines.append(labels['marker'])
    lines += [
        f"Contact: {app.contact_name}",
        f"{labels['organization']}: {app.organization}",
        f"Email: {app.email}",
        f"Phone: {app.phone}",
    ]
    if app.role:
        lines.append(f"Role: {app.role}")
    if app.website:
        lines.append(f"Website: {app.website}")
    if app.location:
        lines.append(f"{labels['location']}: {app.location}")
    if app.org_type:
        lines.append(f"{labels['org_type']}: {app.org_type}")
    if app.services:
        lines.append(f"Services: {', '.join(service_labels(app.services))}")
    if app.volume:
        lines.append(f"{labels['volume']}: {app.volume}")
    if app.start_timing:
        lines.append(f"Start timing: {app.start_timing}")
    if app.countries:
        lines.append(f"Countries: {app.countries}")
    if app.delivery_preference:
        lines.append(f"Delivery preference: {app.delivery_preference}")
    if app.program == Application.PROGRAM_PARTNER:
        lines.append(f"Partner terms accepted: {'yes' if app.partner_terms_ack else 'no'}")
    if app.notes:
        lines.append(f"Notes:\n{app.notes.strip()}")
    lines.append("")
    if app.source_page:
        lines.append(f"Source page: {app.source_page}")
    if app.page_url:
        lines.append(f"Page URL: {app.page_url}")
    lines += _attribution_lines(app)
    lines.append(f"Submitted: {app.created_at:%Y-%m-%d %H:%M} UTC" if app.created_at else "")
    lines.append(f"Application ID: {app.id}")
    return "\n".join(l for l in lines if l is not None).strip()


# =============================================================================
# MANAGER NOTIFICATION
# =============================================================================

def notification_recipients() -> list[str]:
    recipients = [e.strip() for e in (getattr(settings, 'APPLICATIONS_NOTIFY_EMAILS', None) or []) if e.strip()]
    if not recipients:
        recipients = [e.strip() for e in (getattr(settings, 'EMAIL_OFFICE_RECEIVER', None) or []) if e.strip()]
    return recipients


def build_notification_subject(app: Application) -> str:
    if app.program == Application.PROGRAM_PARTNER:
        return f"New Partner application — {app.organization} ({app.location or 'location n/a'})"
    return f"New Business Account application — {app.organization} ({app.org_type or 'type n/a'})"


def admin_url(app: Application) -> str:
    base = getattr(settings, 'BASE_URL', '').rstrip('/')
    return f"{base}/admin/orders/application/{app.id}/change/"


def send_application_notification(app: Application) -> bool:
    """Email the managers. Never raises; returns True when the email was sent."""
    recipients = notification_recipients()
    if not recipients:
        logger.error("No recipients configured for application notifications (APPLICATIONS_NOTIFY_EMAILS / EMAIL_OFFICE_RECEIVER)")
        return False

    labels = PROGRAM_LABELS[app.program]
    body = (
        f"{labels['title']} submitted! Application ID: {app.id}\n\n"
        f"{build_questionnaire(app, include_marker=False)}\n\n"
        f"Admin: {admin_url(app)}\n"
    )
    if app.zoho_lead_id:
        body += f"Zoho lead: {app.zoho_url}\n"
    else:
        body += "Zoho lead: syncing in background (see Admin → Zoho Sync Jobs if it does not appear)\n"

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    thread_prefix = 'partner-applications' if app.program == Application.PROGRAM_PARTNER else 'business-account-applications'
    thread_id = f"<{thread_prefix}-thread-{today_str}@dcmobilenotary.com>"

    try:
        email = EmailMessage(
            subject=build_notification_subject(app),
            body=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@dcmobilenotary.net'),
            to=recipients,
            reply_to=[app.email] if app.email else None,
            headers={
                'Message-ID': f"<application-{app.id}@dcmobilenotary.com>",
                'In-Reply-To': thread_id,
                'References': thread_id,
            },
        )
        email.send()
        Application.objects.filter(pk=app.pk).update(email_sent=True)
        app.email_sent = True
        logger.info("✅ Application notification sent for %s #%s to %s", app.program, app.id, recipients)
        return True
    except Exception:
        logger.exception("Failed to send application notification for %s #%s", app.program, app.id)
        return False


def build_confirmation_subject(app: Application) -> str:
    if app.program == Application.PROGRAM_PARTNER:
        return 'Partner application received — DC Mobile Notary'
    return 'Business Account application received — DC Mobile Notary'


def send_application_confirmation(app: Application) -> bool:
    """HTML confirmation to the applicant (same look as the other client emails). Never raises."""
    from django.template.loader import render_to_string

    if not app.email:
        return False
    labels = PROGRAM_LABELS[app.program]
    context = {
        'title': f"{labels['title']} Received",
        'name': app.contact_name,
        'program_label': 'Partner Program account' if app.program == Application.PROGRAM_PARTNER else 'Business Account',
        'is_partner': app.program == Application.PROGRAM_PARTNER,
        'organization_label': labels['organization'],
        'organization': app.organization,
        'email': app.email,
        'phone': app.phone,
        'role': app.role,
        'website': app.website,
        'location_label': labels['location'],
        'location': app.location,
        'org_type_label': labels['org_type'],
        'org_type': app.org_type,
        'services': ', '.join(service_labels(app.services)),
        'volume_label': labels['volume'],
        'volume': app.volume,
        'start_timing': app.start_timing,
        'countries': app.countries,
        'delivery_preference': app.delivery_preference,
        'notes': app.notes,
        'application_id': app.id,
    }
    html_content = render_to_string('emails/application_confirmation.html', context)
    text_content = (
        f"Hi {app.contact_name},\n\n"
        f"Thank you for your {labels['title'].lower()}. Jules or Daveys will call you within one business day. "
        f"In a hurry? Call (202) 247-0837.\n\n"
        f"{build_questionnaire(app, include_marker=False)}\n\n"
        f"DC Mobile Notary — support@dcmobilenotary.com"
    )
    try:
        email = EmailMultiAlternatives(
            subject=build_confirmation_subject(app),
            body=text_content,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'support@dcmobilenotary.net'),
            to=[app.email],
            reply_to=['support@dcmobilenotary.com'],
            headers={'Message-ID': f"<application-confirmation-{app.id}@dcmobilenotary.com>"},
        )
        email.attach_alternative(html_content, 'text/html')
        email.send()
        Application.objects.filter(pk=app.pk).update(client_email_sent=True)
        app.client_email_sent = True
        logger.info("✅ Application confirmation sent to %s for %s #%s", app.email, app.program, app.id)
        return True
    except Exception:
        logger.exception("Failed to send application confirmation for %s #%s", app.program, app.id)
        return False


# =============================================================================
# CLOUDFLARE TURNSTILE
# =============================================================================

def verify_turnstile(token: str | None, remote_ip: str | None = None) -> bool | None:
    """
    Verify a Turnstile token.

    Returns:
        None  – verification not configured (no TURNSTILE_SECRET_KEY) or no
                token was sent while tokens are optional → caller must allow.
        True  – token valid.
        False – token invalid → caller must reject with 400.
    """
    secret = getattr(settings, 'TURNSTILE_SECRET_KEY', '') or ''
    if not secret:
        return None
    if not token:
        if getattr(settings, 'TURNSTILE_REQUIRE_TOKEN', False):
            return False
        logger.info("Turnstile token missing; allowing because TURNSTILE_REQUIRE_TOKEN is off")
        return None

    payload = {'secret': secret, 'response': token}
    if remote_ip:
        payload['remoteip'] = remote_ip
    try:
        resp = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=(3, 8))
        data = resp.json()
    except Exception:
        # Cloudflare outage must not block real applications.
        logger.exception("Turnstile verification request failed; allowing submission")
        return None
    ok = bool(data.get('success'))
    if not ok:
        logger.warning("Turnstile verification failed: %s", data.get('error-codes'))
    return ok
