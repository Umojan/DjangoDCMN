from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils import timezone
import requests
import logging

logger = logging.getLogger(__name__)

# TrustPilot AFS trigger email (used in BCC)
TRUSTPILOT_TRIGGER_EMAIL = getattr(settings, 'TRUSTPILOT_TRIGGER_EMAIL', 'dcmobilenotary.com+cd7dabbed2@invite.trustpilot.com')

# Google Review URL
GOOGLE_REVIEW_URL = getattr(settings, 'GOOGLE_REVIEW_URL', 'https://g.page/r/YOUR_PLACE_ID/review')

# API name of Leads Won field in Zoho Contacts
ZOHO_LEADS_WON_FIELD = getattr(settings, 'ZOHO_LEADS_WON_FIELD', 'Number_of_Leads_Won')

# Frontend URL for tracking
FRONTEND_URL = getattr(settings, 'FRONTEND_URL', 'https://www.dcmobilenotary.com')


def _get_contact_by_email(email: str, access_token: str) -> dict | None:
    """
    Search for Zoho Contact by email.
    Returns dict with 'id' and leads_won field, or None if not found.
    """
    from orders.zoho_sync import ZOHO_API_DOMAIN
    
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    search_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/search?email={email}"
    
    resp = requests.get(search_url, headers=headers, timeout=30)

    if resp.status_code == 200:
        data = resp.json()
        if 'data' in data and len(data['data']) > 0:
            return data['data'][0]
    elif resp.status_code == 204:
        return None
    else:
        logger.warning(f"Failed to search contact by email {email}: {resp.status_code}")
    
    return None


def _create_contact(name: str, email: str, phone: str, access_token: str) -> str | None:
    """
    Create new Zoho Contact. Returns contact ID or None.
    """
    from orders.zoho_sync import ZOHO_API_DOMAIN
    
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Content-Type": "application/json"
    }
    
    create_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
    payload = {
        "data": [{
            "Last_Name": name or email.split('@')[0],
            "Email": email,
            "Phone": phone or ""
        }]
    }
    
    resp = requests.post(create_url, headers=headers, json=payload, timeout=30)
    
    try:
        data = resp.json()
        if 'data' in data and len(data['data']) > 0:
            item = data['data'][0]
            if 'details' in item:
                return item['details']['id']
            elif item.get('code') == 'DUPLICATE_DATA' and 'details' in item:
                return item['details']['id']
    except Exception as e:
        logger.exception(f"Failed to create contact: {e}")
    
    return None


def _get_service_label(module: str) -> str:
    """Get human-readable service label from module name."""
    labels = {
        'FBI': 'FBI Apostille',
        'FBI_Background_Checks': 'FBI Apostille',
        'Deals': 'FBI Apostille',
        'Triple_Seal_Apostilles': 'Triple Seal Marriage',
        'Triple Seal': 'Triple Seal Marriage',
        'Marriage': 'Triple Seal Marriage',
        'Embassy_Legalization': 'Embassy Legalization',
        'Embassy Legalization': 'Embassy Legalization',
        'Embassy': 'Embassy Legalization',
        'Apostille_Services': 'Apostille',
        'Apostille': 'Apostille',
        'Translation_Services': 'Translation',
        'Translation': 'Translation',
        'I_9_Verification': 'I-9 Verification',
        'I-9': 'I-9 Verification',
        'Notary': 'Notary Service',
    }
    return labels.get(module, module or 'Notary Service')


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def process_review_request_task(self, review_request_id: int):
    """
    Main task for processing review request:
    1. Get or create Contact in Zoho (if contact_id not provided)
    2. Get Leads_Won from Contact
    3. Determine review type (Google or TrustPilot)
    4. Update Leads_Won in Zoho (+1)
    5. Send "Order Completed" email with review request
    """
    from .models import ReviewRequest
    from orders.zoho_sync import get_access_token, ZOHO_API_DOMAIN

    logger.info(f"🔄 Starting review task for id={review_request_id}")

    try:
        review_request = ReviewRequest.objects.get(id=review_request_id)
    except ReviewRequest.DoesNotExist:
        logger.error(f"ReviewRequest {review_request_id} not found")
        return

    # Skip already processed reviews (prevents re-processing on retry)
    if review_request.is_sent:
        logger.info(f"ReviewRequest {review_request_id} already sent, skipping")
        return

    try:
        logger.info(f"Getting Zoho access token...")
        access_token = get_access_token()
        logger.info(f"Got token, processing {review_request.email}")
        
        # 1. Get or find Contact ID
        contact_id = review_request.zoho_contact_id
        leads_won = 0
        
        if not contact_id:
            logger.info(f"No contact_id provided, searching by email: {review_request.email}")
            contact = _get_contact_by_email(review_request.email, access_token)
            
            if contact:
                contact_id = contact.get('id')
                leads_won_value = contact.get(ZOHO_LEADS_WON_FIELD)
                if leads_won_value is not None:
                    try:
                        leads_won = int(leads_won_value)
                    except (ValueError, TypeError):
                        leads_won = 0
                logger.info(f"Found contact {contact_id} with {ZOHO_LEADS_WON_FIELD}={leads_won}")
            else:
                logger.info(f"Contact not found, creating new one for {review_request.email}")
                contact_id = _create_contact(
                    review_request.name,
                    review_request.email,
                    review_request.phone,
                    access_token
                )
                leads_won = 0
                logger.info(f"Created new contact: {contact_id}")
            
            if contact_id:
                review_request.zoho_contact_id = contact_id
                review_request.save(update_fields=['zoho_contact_id'])
        else:
            # Contact ID provided, fetch leads_won from Zoho
            headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            contact_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/{contact_id}"
            resp = requests.get(contact_url, headers=headers, params={'fields': ZOHO_LEADS_WON_FIELD}, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and len(data['data']) > 0:
                    leads_won_value = data['data'][0].get(ZOHO_LEADS_WON_FIELD)
                    if leads_won_value is not None:
                        try:
                            leads_won = int(leads_won_value)
                        except (ValueError, TypeError):
                            leads_won = 0
        
        if not contact_id:
            logger.error(f"Could not get or create contact for {review_request.email}")
            return
        
        review_request.leads_won_before = leads_won
        
        # 2. Determine review type
        # Leads Won = 0 -> first customer -> Google Review
        # Leads Won >= 1 -> returning customer -> TrustPilot
        if leads_won == 0:
            review_request.review_type = 'google'
            new_leads_won = 1
        else:
            review_request.review_type = 'trustpilot'
            new_leads_won = leads_won + 1
        
        review_request.leads_won_after = new_leads_won
        review_request.save(update_fields=['leads_won_before', 'leads_won_after', 'review_type'])
        
        logger.info(f"ReviewRequest {review_request_id}: type={review_request.review_type}, "
                    f"leads_won {leads_won} -> {new_leads_won}")
        
        # 3. Update Leads_Won in Zoho Contact
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }
        
        update_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
        update_payload = {
            "data": [{
                "id": contact_id,
                ZOHO_LEADS_WON_FIELD: new_leads_won
            }]
        }
        update_resp = requests.put(update_url, headers=headers, json=update_payload, timeout=30)
        
        if update_resp.status_code in (200, 201):
            logger.info(f"Updated {ZOHO_LEADS_WON_FIELD}={new_leads_won} for contact {contact_id}")
        else:
            logger.warning(f"Failed to update Leads_Won in Zoho: {update_resp.status_code} {update_resp.text}")
        
        # 4. Send review request email
        if review_request.review_type == 'google':
            _send_google_review_email(review_request)
        else:
            _send_trustpilot_email(review_request)
        
        # Success
        review_request.is_sent = True
        review_request.sent_at = timezone.now()
        review_request.save(update_fields=['is_sent', 'sent_at'])
        
        logger.info(f"✅ ReviewRequest {review_request_id} processed: "
                    f"{review_request.review_type} sent to {review_request.email}")
        
    except Exception as e:
        logger.exception(f"❌ Failed to process ReviewRequest {review_request_id}: {e}")
        raise self.retry(exc=e)


def _send_review_request_email(review_request, template='emails/review_thank_you.html', subject_prefix='Order Completed'):
    """Send the "Order Completed" email with the 5 star-rating links (gated review flow).

    Same email for first-time and returning customers. The public platform (Google vs
    Trustpilot) is decided only after a positive click — see reviews/services.py.
    Trustpilot AFS is NOT bcc'd here any more; the invite goes out after a positive rating.
    """
    from .services import star_links

    tracking_url = None
    if review_request.tracking_id:
        tracking_url = f"{FRONTEND_URL}/tracking?tid={review_request.tracking_id}"

    service_label = _get_service_label(review_request.zoho_module)
    html_content = render_to_string(template, {
        'name': review_request.first_name or 'Valued Customer',
        'service_label': service_label,
        'tid': review_request.tracking_id,
        'tracking_url': tracking_url,
        'stars': star_links(review_request),
    })

    subject = f"{subject_prefix} — {service_label}"
    if review_request.tracking_id:
        subject = f"{subject} — {review_request.tracking_id}"

    msg = EmailMessage(
        subject=subject,
        body=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[review_request.email],
    )
    msg.content_subtype = 'html'
    msg.send(fail_silently=False)

    if not review_request.stars_email_sent:
        review_request.stars_email_sent = True
        review_request.save(update_fields=['stars_email_sent'])

    logger.info(f"📧 Review request email ({review_request.review_type or 'n/a'}) sent to {review_request.email}")


# Backwards-compatible names (management command, old call sites)
def _send_google_review_email(review_request):
    _send_review_request_email(review_request)


def _send_trustpilot_email(review_request):
    _send_review_request_email(review_request)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def notify_negative_task(self, review_id: int):
    """Manager email + Zoho note/task for a negative rating. Idempotent (see services.notify_negative)."""
    from .models import Review
    from .services import notify_negative

    try:
        review = Review.objects.select_related('request').get(id=review_id)
    except Review.DoesNotExist:
        logger.error(f"Review {review_id} not found")
        return
    if review.is_positive:
        return
    try:
        result = notify_negative(review)
        logger.info(f"Review {review_id}: negative notification → {result}")
    except Exception as e:
        logger.exception(f"Review {review_id}: negative notification failed: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def send_trustpilot_invite_task(self, review_request_id: int):
    """Positive rating from a returning customer → short thank-you email bcc'd to Trustpilot AFS."""
    from .models import ReviewRequest
    from .services import send_trustpilot_invite

    try:
        rr = ReviewRequest.objects.get(id=review_request_id)
    except ReviewRequest.DoesNotExist:
        return
    try:
        if send_trustpilot_invite(rr):
            logger.info(f"📧 Trustpilot invite sent for ReviewRequest {review_request_id}")
    except Exception as e:
        logger.exception(f"Trustpilot invite failed for {review_request_id}: {e}")
        raise self.retry(exc=e)


@shared_task
def send_review_reminders():
    """Celery beat (daily): one gentle reminder to customers who never clicked a star.

    Only requests that got the star email (stars_email_sent) between REVIEWS_REMINDER_DAYS and
    REVIEWS_REMINDER_MAX_DAYS ago, with no Review and no reminder yet.
    """
    from django.utils import timezone as tz
    from datetime import timedelta
    from .models import ReviewRequest

    days = int(getattr(settings, 'REVIEWS_REMINDER_DAYS', 3))
    max_days = int(getattr(settings, 'REVIEWS_REMINDER_MAX_DAYS', 14))
    if days <= 0:
        return 0
    now = tz.now()
    qs = ReviewRequest.objects.filter(
        is_sent=True,
        stars_email_sent=True,
        reminder_sent_at__isnull=True,
        review__isnull=True,
        sent_at__lte=now - timedelta(days=days),
        sent_at__gte=now - timedelta(days=max_days),
    ).order_by('sent_at')[:200]

    sent = 0
    for rr in qs:
        try:
            _send_review_request_email(rr, template='emails/review_reminder.html', subject_prefix='Quick question about your order')
            rr.reminder_sent_at = now
            rr.save(update_fields=['reminder_sent_at'])
            sent += 1
        except Exception as e:
            logger.exception(f"Review reminder failed for {rr.id}: {e}")
    logger.info(f"Review reminders sent: {sent}")
    return sent
