from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils import timezone
import requests
import logging

logger = logging.getLogger(__name__)

# TrustPilot AFS trigger email
TRUSTPILOT_TRIGGER_EMAIL = getattr(settings, 'TRUSTPILOT_TRIGGER_EMAIL', 'dcmobilenotary.com+cd7dabbed2@invite.trustpilot.com')

# Google Review URL - can be overridden in settings.py
GOOGLE_REVIEW_URL = getattr(settings, 'GOOGLE_REVIEW_URL', 'https://g.page/r/YOUR_PLACE_ID/review')

# API name of Leads Won field in Zoho Contacts
ZOHO_LEADS_WON_FIELD = getattr(settings, 'ZOHO_LEADS_WON_FIELD', 'Number_of_Leads_Won')


def _get_contact_by_email(email: str, access_token: str) -> dict | None:
    """
    Search for Zoho Contact by email.
    Returns dict with 'id' and leads_won field, or None if not found.
    """
    from orders.zoho_sync import ZOHO_API_DOMAIN
    
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    search_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/search?email={email}"
    
    resp = requests.get(search_url, headers=headers)
    
    if resp.status_code == 200:
        data = resp.json()
        if 'data' in data and len(data['data']) > 0:
            return data['data'][0]
    elif resp.status_code == 204:
        # No content - contact not found
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
    
    resp = requests.post(create_url, headers=headers, json=payload)
    
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


@shared_task
def process_review_request_task(review_request_id: int):
    """
    Main task for processing review request:
    1. Get or create Contact in Zoho (if contact_id not provided)
    2. Get Leads_Won from Contact
    3. Determine review type (Google or TrustPilot)
    4. Update Leads_Won in Zoho (+1)
    5. Send corresponding review request
    """
    from .models import ReviewRequest
    from orders.zoho_sync import get_access_token, ZOHO_API_DOMAIN
    
    try:
        review_request = ReviewRequest.objects.get(id=review_request_id)
    except ReviewRequest.DoesNotExist:
        logger.error(f"ReviewRequest {review_request_id} not found")
        return
    
    try:
        access_token = get_access_token()
        
        # 1. Get or find Contact ID
        contact_id = review_request.zoho_contact_id
        leads_won = 0
        
        if not contact_id:
            # Try to find contact by email
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
                # Create new contact
                logger.info(f"Contact not found, creating new one for {review_request.email}")
                contact_id = _create_contact(
                    review_request.name,
                    review_request.email,
                    review_request.phone,
                    access_token
                )
                leads_won = 0
                logger.info(f"Created new contact: {contact_id}")
            
            # Save contact_id to review request
            if contact_id:
                review_request.zoho_contact_id = contact_id
                review_request.save(update_fields=['zoho_contact_id'])
        else:
            # Contact ID provided, fetch leads_won from Zoho
            headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            contact_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/{contact_id}"
            resp = requests.get(contact_url, headers=headers, params={'fields': ZOHO_LEADS_WON_FIELD})
            
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
        update_resp = requests.put(update_url, headers=headers, json=update_payload)
        
        if update_resp.status_code in (200, 201):
            logger.info(f"Updated {ZOHO_LEADS_WON_FIELD}={new_leads_won} for contact {contact_id}")
        else:
            logger.warning(f"Failed to update Leads_Won in Zoho: {update_resp.status_code} {update_resp.text}")
        
        # 4. Send review request
        if review_request.review_type == 'google':
            _send_google_review(review_request)
        else:
            _send_trustpilot_invite(review_request)
        
        # Success
        review_request.is_sent = True
        review_request.sent_at = timezone.now()
        review_request.save(update_fields=['is_sent', 'sent_at'])
        
        logger.info(f"✅ ReviewRequest {review_request_id} processed: "
                    f"{review_request.review_type} sent to {review_request.email}")
        
    except Exception as e:
        logger.exception(f"❌ Failed to process ReviewRequest {review_request_id}: {e}")


def _send_google_review(review_request):
    """Send email asking customer to leave a Google Review."""
    try:
        html_content = render_to_string('emails/google_review_request.html', {
            'name': review_request.name or 'Valued Customer',
            'review_url': GOOGLE_REVIEW_URL,
        })
    except Exception as e:
        # Fallback if template not found
        logger.warning(f"Template not found, using fallback: {e}")
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2>Thank you, {review_request.name or 'Valued Customer'}! 🎉</h2>
            <p>We hope you had a great experience with DC Mobile Notary!</p>
            <p>Your feedback helps us improve and helps others find reliable notary services.</p>
            <p><a href="{GOOGLE_REVIEW_URL}" style="display: inline-block; padding: 12px 24px; 
               background-color: #4285f4; color: white; text-decoration: none; border-radius: 5px;">
               ⭐ Leave a Google Review</a></p>
            <p>Thank you for choosing DC Mobile Notary!</p>
        </body>
        </html>
        """
    
    msg = EmailMessage(
        subject='Thank you for choosing DC Mobile Notary! 🌟',
        body=html_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[review_request.email],
    )
    msg.content_subtype = 'html'
    msg.send(fail_silently=False)
    
    logger.info(f"📧 Google Review email sent to {review_request.email}")


def _send_trustpilot_invite(review_request):
    """
    Trigger TrustPilot AFS to send a verified review invitation.
    TrustPilot will send the email to the customer.
    """
    reference_id = review_request.tracking_id or review_request.zoho_deal_id or str(review_request.id)
    
    msg = EmailMessage(
        subject=review_request.email,  # Customer email in subject
        body=f"referenceId: {reference_id}\n"
             f"name: {review_request.name}\n"
             f"email: {review_request.email}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[TRUSTPILOT_TRIGGER_EMAIL],
        reply_to=[review_request.email],
    )
    msg.send(fail_silently=False)
    
    logger.info(f"📧 TrustPilot invite triggered for {review_request.email} (ref: {reference_id})")
