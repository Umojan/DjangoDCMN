# orders/tasks.py
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder, MarriageOrder, \
    I9VerificationOrder, QuoteRequest
from .zoho_sync import (
    sync_fbi_order_to_zoho,
    sync_embassy_order_to_zoho,
    sync_translation_order_to_zoho,
    sync_apostille_order_to_zoho,
    sync_marriage_order_to_zoho,
    sync_i9_order_to_zoho, sync_quote_request_to_zoho,
    update_record_fields,
)
from .models import Track
from .utils import service_label


@shared_task
def test_celery_task():
    print("Celery is working!")
    return "Hello from Celery"


@shared_task
def sync_order_to_zoho_task(order_id, order_type, tracking_id=None):
    try:
        if order_type == "fbi":
            order = FbiApostilleOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_fbi_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "embassy":
            order = EmbassyLegalizationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_embassy_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "apostille":
            order = ApostilleOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_apostille_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "translation":
            order = TranslationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_translation_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "marriage":
            order = MarriageOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_marriage_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "I-9":
            order = I9VerificationOrder.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_i9_order_to_zoho(order, tracking_id=tracking_id)
        elif order_type == "quote":
            order = QuoteRequest.objects.get(id=order_id)
            if not order.zoho_synced:
                sync_quote_request_to_zoho(order)
    except Exception as e:
        print(f"[Celery Task Error] Failed to sync {order_type} order #{order_id} to Zoho: {e}")


@shared_task
def write_tracking_id_to_zoho_task(module_name: str, record_id: str, tracking_id: str) -> bool:
    """Persist TID to Zoho record using configured custom field name.
    Adjust the field key below to your Zoho module custom field.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Example: custom field API name 'Tracking_ID'
    fields = {"Tracking_ID": tracking_id}
    logger.info(f"[Celery] write_tracking_id_to_zoho_task: module={module_name}, record_id={record_id}, tid={tracking_id}")
    
    try:
        ok = update_record_fields(module_name, record_id, fields)
        if not ok:
            logger.error(f"[Celery] ❌ Failed to write Tracking_ID={tracking_id} to Zoho {module_name}/{record_id}")
        else:
            logger.info(f"[Celery] ✅ Successfully wrote Tracking_ID={tracking_id} to Zoho {module_name}/{record_id}")
        return ok
    except Exception as e:
        logger.exception(f"[Celery] Exception writing Tracking_ID={tracking_id} to {module_name}/{record_id}: {e}")
        return False


@shared_task
def send_tracking_email_task(tid: str, stage_code: str):
    """
    Отправляет HTML email с обновлением статуса заказа.
    Все письма группируются в одну ветку по TID.
    """
    from django.template.loader import render_to_string
    from django.core.mail import EmailMessage
    import logging
    
    logger = logging.getLogger(__name__)
    
    track = Track.objects.filter(tid=tid).first()
    if not track:
        logger.warning(f"Track not found for TID: {tid}")
        return
    
    data = track.data or {}
    email = data.get('email')
    name = data.get('name', 'Customer')
    
    if not email:
        logger.warning(f"No email found for TID: {tid}")
        return

    svc = service_label(track.service)
    
    # Определяем заголовок и сообщение в зависимости от стадии
    if stage_code == 'created':
        title = "We Received Your Order! 📋"
        message = "Thank you for choosing DC Mobile Notary! We have received your order and will begin processing it shortly."
    elif stage_code == 'document_received':
        title = "Documents Received ✅"
        message = "We have successfully received your documents and they are now in our processing queue."
    elif stage_code == 'notarized':
        title = "Documents Notarized 📝"
        message = "Your documents have been notarized and are ready for the next step in the apostille process."
    elif stage_code == 'submitted':
        title = "Submitted to State Authority 🏛️"
        message = "Your documents have been submitted to the appropriate state authority for authentication."
    elif stage_code == 'processed_dos':
        title = "Processed at U.S. Department of State ✅"
        message = "Great news! Your documents have been processed by the U.S. Department of State."
    elif stage_code == 'processed_state':
        title = "Processed at State Authority ✅"
        message = "Your documents have been successfully processed by the state authority."
    elif stage_code == 'state_authenticated':
        title = "State Authentication Complete ✅"
        message = "Your documents have been authenticated at the state level."
    elif stage_code == 'federal_authenticated':
        title = "Federal Authentication Complete ✅"
        message = "Your documents have been authenticated by the U.S. Department of State."
    elif stage_code == 'embassy_legalized':
        title = "Embassy Legalization Complete ✅"
        message = "Your documents have been legalized by the embassy/consulate."
    elif stage_code == 'translated':
        title = "Translation Complete 🌐"
        message = "Your documents have been professionally translated and are ready for delivery."
    elif stage_code == 'quality_approved':
        title = "Quality Check Approved ✅"
        message = "Your translation has passed our quality assurance review."
    elif stage_code == 'delivered':
        title = "Order Delivered! 🎉"
        message = "Your order has been delivered! We hope you're satisfied with our service."
    else:
        title = "Order Update"
        message = f"Your order status has been updated to: {stage_code}"
    
    # Получаем текущую стадию и комментарий
    from .constants import STAGE_DEFS
    current_stage_name = ""
    comment = data.get('comment', '')
    
    defs = STAGE_DEFS.get(track.service, [])
    for stage_def in defs:
        if stage_def['code'] == stage_code:
            current_stage_name = stage_def['name']
            break
    
    # URL для трекинга
    tracking_url = f"{settings.FRONTEND_URL}/tracking?tid={tid}"
    
    # Рендерим HTML шаблон
    html_content = render_to_string('emails/tracking_update.html', {
        'title': title,
        'name': name,
        'message': message,
        'current_stage': current_stage_name,
        'comment': comment,
        'service_label': svc,
        'tid': tid,
        'shipping': data.get('shipping', ''),
        'tracking_url': tracking_url,
    })
    
    # Формируем subject - одинаковый для всей ветки
    subject = f"Order Status: {svc} — {tid}"
    
    # Email threading: все письма по одному TID в одной ветке
    # Первое письмо (created) создает ветку, остальные отвечают на него
    thread_id = f"<tracking-{tid}@dcmobilenotary.com>"
    message_id = f"<tracking-{tid}-{stage_code}@dcmobilenotary.com>"
    
    headers = {
        'Message-ID': message_id,
    }
    
    # Для всех писем кроме первого добавляем In-Reply-To и References
    if stage_code != 'created':
        headers['In-Reply-To'] = thread_id
        headers['References'] = thread_id
    else:
        # Первое письмо само становится корнем ветки
        headers['Message-ID'] = thread_id
    
    try:
        email_message = EmailMessage(
            subject=subject,
            body=html_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
            headers=headers
        )
        email_message.content_subtype = 'html'
        email_message.send(fail_silently=False)
        logger.info(f"✅ Tracking email sent to {email} for TID {tid}, stage: {stage_code}")
    except Exception as e:
        logger.exception(f"❌ Failed to send tracking email for TID {tid}: {e}")