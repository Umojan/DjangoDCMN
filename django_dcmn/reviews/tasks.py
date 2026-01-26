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


@shared_task
def process_review_request_task(review_request_id: int):
    """
    Main task for processing review request:
    1. Determine review type (Google or TrustPilot) based on leads_won_before
    2. Update Leads_Won in Zoho (+1)
    3. Send corresponding review request
    """
    from .models import ReviewRequest
    from orders.zoho_sync import get_access_token, ZOHO_API_DOMAIN
    
    try:
        review_request = ReviewRequest.objects.get(id=review_request_id)
    except ReviewRequest.DoesNotExist:
        logger.error(f"ReviewRequest {review_request_id} not found")
        return
    
    try:
        # leads_won_before already received from webhook
        leads_won = review_request.leads_won_before
        
        # 1. Determine review type
        # Leads Won = 0 -> first customer -> Google Review
        # Leads Won >= 1 -> returning customer -> TrustPilot
        if leads_won == 0:
            review_request.review_type = 'google'
            new_leads_won = 1
        else:
            review_request.review_type = 'trustpilot'
            new_leads_won = leads_won + 1
        
        review_request.leads_won_after = new_leads_won
        review_request.save(update_fields=['leads_won_after', 'review_type'])
        
        logger.info(f"ReviewRequest {review_request_id}: type={review_request.review_type}, "
                    f"leads_won {leads_won} -> {new_leads_won}")
        
        # 2. Update Leads_Won in Zoho Contact
        access_token = get_access_token()
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json"
        }
        
        update_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
        update_payload = {
            "data": [{
                "id": review_request.zoho_contact_id,
                ZOHO_LEADS_WON_FIELD: new_leads_won
            }]
        }
        update_resp = requests.put(update_url, headers=headers, json=update_payload)
        
        if update_resp.status_code in (200, 201):
            logger.info(f"Updated {ZOHO_LEADS_WON_FIELD}={new_leads_won} for contact {review_request.zoho_contact_id}")
        else:
            logger.warning(f"Failed to update Leads_Won in Zoho: {update_resp.status_code} {update_resp.text}")
        
        # 3. Send review request
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
