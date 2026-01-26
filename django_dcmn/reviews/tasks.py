from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils import timezone
import requests
import logging

logger = logging.getLogger(__name__)

# TrustPilot AFS trigger email
TRUSTPILOT_TRIGGER_EMAIL = 'dcmobilenotary.com+cd7dabbed2@invite.trustpilot.com'

# Google Review URL - можно переопределить в settings.py
GOOGLE_REVIEW_URL = getattr(settings, 'GOOGLE_REVIEW_URL', 'https://g.page/r/YOUR_PLACE_ID/review')

# API имя поля Leads Won в Zoho Contacts - можно переопределить в settings.py
ZOHO_LEADS_WON_FIELD = getattr(settings, 'ZOHO_LEADS_WON_FIELD', 'Number_of_Leads_Won')


@shared_task
def process_review_request_task(review_request_id: int):
    """
    Основной task для обработки review request:
    1. Получить Leads_Won из Zoho Contact
    2. Определить тип review (Google или TrustPilot)
    3. Обновить Leads_Won в Zoho (+1)
    4. Отправить соответствующий review request
    """
    from .models import ReviewRequest
    from orders.zoho_sync import get_access_token, ZOHO_API_DOMAIN
    
    try:
        review_request = ReviewRequest.objects.get(id=review_request_id)
    except ReviewRequest.DoesNotExist:
        logger.error(f"ReviewRequest {review_request_id} not found")
        return
    
    try:
        # 1. Получаем текущее значение Leads_Won из Zoho Contact
        access_token = get_access_token()
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        
        contact_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts/{review_request.zoho_contact_id}"
        resp = requests.get(contact_url, headers=headers, params={'fields': ZOHO_LEADS_WON_FIELD})
        
        leads_won = 0
        if resp.status_code == 200:
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                raw_value = data['data'][0].get(ZOHO_LEADS_WON_FIELD)
                if raw_value is not None:
                    try:
                        leads_won = int(raw_value)
                    except (ValueError, TypeError):
                        leads_won = 0
                logger.info(f"Contact {review_request.zoho_contact_id}: {ZOHO_LEADS_WON_FIELD}={leads_won}")
        elif resp.status_code == 401:
            # Token expired, refresh and retry
            access_token = get_access_token(force_refresh=True)
            headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            resp = requests.get(contact_url, headers=headers, params={'fields': ZOHO_LEADS_WON_FIELD})
            if resp.status_code == 200:
                data = resp.json()
                if 'data' in data and len(data['data']) > 0:
                    raw_value = data['data'][0].get(ZOHO_LEADS_WON_FIELD)
                    if raw_value is not None:
                        try:
                            leads_won = int(raw_value)
                        except (ValueError, TypeError):
                            leads_won = 0
        else:
            logger.warning(f"Failed to get contact {review_request.zoho_contact_id}: {resp.status_code} {resp.text}")
        
        review_request.leads_won_before = leads_won
        
        # 2. Определяем тип review
        # Leads Won = 0 или пусто → первый клиент → Google Review
        # Leads Won >= 1 → повторный клиент → TrustPilot
        if leads_won == 0:
            review_request.review_type = 'google'
            new_leads_won = 1
        else:
            review_request.review_type = 'trustpilot'
            new_leads_won = leads_won + 1
        
        review_request.leads_won_after = new_leads_won
        review_request.save(update_fields=['leads_won_before', 'leads_won_after', 'review_type'])
        
        logger.info(f"ReviewRequest {review_request_id}: type={review_request.review_type}, "
                    f"leads_won {leads_won} → {new_leads_won}")
        
        # 3. Обновляем Leads_Won в Zoho Contact
        update_url = f"{ZOHO_API_DOMAIN}/crm/v2/Contacts"
        update_payload = {
            "data": [{
                "id": review_request.zoho_contact_id,
                ZOHO_LEADS_WON_FIELD: new_leads_won
            }]
        }
        headers["Content-Type"] = "application/json"
        update_resp = requests.put(update_url, headers=headers, json=update_payload)
        
        if update_resp.status_code in (200, 201):
            logger.info(f"Updated {ZOHO_LEADS_WON_FIELD}={new_leads_won} for contact {review_request.zoho_contact_id}")
        else:
            logger.warning(f"Failed to update Leads_Won in Zoho: {update_resp.status_code} {update_resp.text}")
        
        # 4. Отправляем review request
        if review_request.review_type == 'google':
            _send_google_review(review_request)
        else:
            _send_trustpilot_invite(review_request)
        
        # Успех
        review_request.is_sent = True
        review_request.sent_at = timezone.now()
        review_request.save(update_fields=['is_sent', 'sent_at'])
        
        logger.info(f"✅ ReviewRequest {review_request_id} processed: "
                    f"{review_request.review_type} sent to {review_request.email}")
        
    except Exception as e:
        logger.exception(f"❌ Failed to process ReviewRequest {review_request_id}: {e}")


def _send_google_review(review_request):
    """Отправляет email с просьбой оставить Google Review."""
    try:
        html_content = render_to_string('emails/google_review_request.html', {
            'name': review_request.name or 'Valued Customer',
            'review_url': GOOGLE_REVIEW_URL,
        })
    except Exception as e:
        # Fallback если шаблон не найден
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
    Триггерит TrustPilot AFS для отправки верифицированного приглашения.
    TrustPilot сам отправит email клиенту.
    
    Формат зависит от настроек TrustPilot AFS:
    - Email клиента обычно в subject или body
    - Reference ID для идентификации заказа
    """
    # Формат для TrustPilot AFS
    reference_id = review_request.tracking_id or review_request.zoho_deal_id or str(review_request.id)
    
    msg = EmailMessage(
        subject=review_request.email,  # Email клиента в subject
        body=f"referenceId: {reference_id}\n"
             f"name: {review_request.name}\n"
             f"email: {review_request.email}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[TRUSTPILOT_TRIGGER_EMAIL],
        reply_to=[review_request.email],
    )
    msg.send(fail_silently=False)
    
    logger.info(f"📧 TrustPilot invite triggered for {review_request.email} (ref: {reference_id})")
