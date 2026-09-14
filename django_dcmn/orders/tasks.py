# orders/tasks.py
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import FbiApostilleOrder, EmbassyLegalizationOrder, TranslationOrder, ApostilleOrder, MarriageOrder, \
    I9VerificationOrder, QuoteRequest, PreCheckSubmission, FingerprintingSubmission, PhoneCallLead, ZohoSyncJob, \
    Application
from .zoho_sync import (
    EXTERNAL_ORDER_KEY_FIELD,
    get_order_external_key,
    sync_fbi_order_to_zoho,
    sync_embassy_order_to_zoho,
    sync_translation_order_to_zoho,
    sync_apostille_order_to_zoho,
    sync_marriage_order_to_zoho,
    sync_i9_order_to_zoho, sync_quote_request_to_zoho,
    sync_precheck_to_zoho,
    sync_fingerprinting_to_zoho,
    sync_application_to_zoho,
    sync_order_attachments,
    update_record_fields,
)
from .zoho_errors import ZohoPermanentError, ZohoTransientError
from .models import Track
from .utils import service_label
import datetime
import logging

logger = logging.getLogger(__name__)


def sync_phone_lead_to_zoho(phone_lead):
    from .services.whatconverts_zoho import sync_phone_lead_to_zoho as sync_phone

    return sync_phone(phone_lead)


ORDER_TYPE_ALIASES = {
    'I-9': 'i9',
    'i-9': 'i9',
}

ORDER_TYPE_MAP = {
    'fbi': (FbiApostilleOrder, sync_fbi_order_to_zoho),
    'embassy': (EmbassyLegalizationOrder, sync_embassy_order_to_zoho),
    'apostille': (ApostilleOrder, sync_apostille_order_to_zoho),
    'translation': (TranslationOrder, sync_translation_order_to_zoho),
    'marriage': (MarriageOrder, sync_marriage_order_to_zoho),
    'i9': (I9VerificationOrder, sync_i9_order_to_zoho),
    'quote': (QuoteRequest, sync_quote_request_to_zoho),
    'pre-check': (PreCheckSubmission, sync_precheck_to_zoho),
    'fingerprinting': (FingerprintingSubmission, sync_fingerprinting_to_zoho),
    'phone': (PhoneCallLead, sync_phone_lead_to_zoho),
    'application': (Application, sync_application_to_zoho),
}


def canonical_order_type(order_type):
    return ORDER_TYPE_ALIASES.get(order_type, order_type)


def matched_order_type_aliases(order_type):
    canonical = canonical_order_type(order_type)
    if canonical == 'i9':
        return ('i9', 'I-9', 'i-9')
    return (canonical,)


def enqueue_zoho_sync(order_id, order_type, tracking_id=None):
    """Persist intent before publishing so a broker failure cannot lose the sync."""
    canonical = canonical_order_type(order_type)
    if canonical not in ORDER_TYPE_MAP:
        raise ValueError(f'Unknown order_type: {order_type}')

    job, _ = ZohoSyncJob.objects.get_or_create(
        order_type=canonical,
        order_id=order_id,
        defaults={
            'tracking_id': tracking_id or '',
            'status': ZohoSyncJob.STATUS_PENDING,
        },
    )
    if job.status in (ZohoSyncJob.STATUS_SYNCED, ZohoSyncJob.STATUS_SUPPRESSED):
        return job

    changed_fields = []
    if tracking_id and job.tracking_id != tracking_id:
        job.tracking_id = tracking_id
        changed_fields.append('tracking_id')
    if job.status == ZohoSyncJob.STATUS_FAILED:
        job.status = ZohoSyncJob.STATUS_PENDING
        changed_fields.append('status')
    if changed_fields:
        job.save(update_fields=changed_fields + ['updated_at'])

    def publish():
        try:
            sync_order_to_zoho_task.delay(
                order_id,
                canonical,
                tracking_id=tracking_id or job.tracking_id or None,
            )
        except Exception as exc:
            logger.exception(
                'Failed to publish Zoho sync for %s #%s',
                canonical,
                order_id,
            )
            ZohoSyncJob.objects.filter(pk=job.pk).update(
                status=ZohoSyncJob.STATUS_PENDING,
                last_error=f'Queue publish failed: {exc}'[:2000],
            )

    transaction.on_commit(publish)
    return job


@shared_task
def test_celery_task():
    print("Celery is working!")
    return "Hello from Celery"


@shared_task(
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(ZohoTransientError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={'max_retries': 8},
)
def sync_order_to_zoho_task(self, order_id, order_type, tracking_id=None):
    canonical = canonical_order_type(order_type)
    entry = ORDER_TYPE_MAP.get(canonical)
    if not entry:
        logger.error('[Celery] Unknown order_type: %s', order_type)
        return

    model_class, sync_func = entry
    try:
        with transaction.atomic():
            order = model_class.objects.select_for_update().get(id=order_id)
            job, _ = ZohoSyncJob.objects.select_for_update().get_or_create(
                order_type=canonical,
                order_id=order_id,
                defaults={'tracking_id': tracking_id or ''},
            )
            if job.status == ZohoSyncJob.STATUS_SUPPRESSED:
                logger.info(
                    '[Celery] Zoho sync suppressed for %s #%s',
                    canonical,
                    order_id,
                )
                return

            sync_complete = order.zoho_synced
            if canonical == 'phone':
                sync_complete = (
                    sync_complete
                    and bool(order.zoho_module)
                    and bool(order.zoho_lead_id)
                    and bool(order.zoho_attribution_id)
                )
            if sync_complete:
                job.status = ZohoSyncJob.STATUS_SYNCED
                if canonical == 'phone':
                    job.zoho_module = order.zoho_module
                    job.zoho_record_id = order.zoho_lead_id
                job.synced_at = job.synced_at or timezone.now()
                job.last_error = ''
                job.save(
                    update_fields=[
                        'status',
                        'zoho_module',
                        'zoho_record_id',
                        'synced_at',
                        'last_error',
                        'updated_at',
                    ]
                )
                logger.info(
                    '[Celery] Order %s #%s already synced, skipping',
                    canonical,
                    order_id,
                )
                return

            job.status = ZohoSyncJob.STATUS_RUNNING
            job.attempts += 1
            job.last_attempt_at = timezone.now()
            job.last_error = ''
            if tracking_id:
                job.tracking_id = tracking_id
            job.save(
                update_fields=[
                    'status',
                    'attempts',
                    'last_attempt_at',
                    'last_error',
                    'tracking_id',
                    'updated_at',
                ]
            )

            matched_phone_lead = None
            if canonical != 'phone':
                matched_phone_lead = PhoneCallLead.objects.filter(
                    matched_order_type__in=matched_order_type_aliases(canonical),
                    matched_order_id=order.id,
                ).first()

            if canonical == 'phone':
                result = sync_func(order)
                if not result:
                    raise ZohoTransientError(
                        f'Zoho sync returned false for phone #{order_id}'
                    )
                order.refresh_from_db(
                    fields=[
                        'zoho_module',
                        'zoho_lead_id',
                        'zoho_attribution_id',
                        'zoho_synced',
                    ]
                )
                if not (
                    order.zoho_synced
                    and order.zoho_module
                    and order.zoho_lead_id
                    and order.zoho_attribution_id
                ):
                    raise ZohoTransientError(
                        f'Phone sync incomplete for phone #{order_id}'
                    )
                job.zoho_module = order.zoho_module
                job.zoho_record_id = order.zoho_lead_id
            elif matched_phone_lead:
                if not matched_phone_lead.zoho_lead_id:
                    try:
                        sync_order_to_zoho_task.delay(
                            matched_phone_lead.id,
                            'phone',
                        )
                    except Exception:
                        logger.exception(
                            '[Celery] Failed to wake phone sync #%s',
                            matched_phone_lead.id,
                        )
                    raise ZohoTransientError(
                        f'Waiting for matched phone lead '
                        f'#{matched_phone_lead.id} to finish Zoho sync'
                    )

                from .services.zoho_update import update_matched_zoho_record

                ok = update_matched_zoho_record(
                    order,
                    canonical,
                    tracking_id=tracking_id or job.tracking_id or None,
                )
                if not ok:
                    raise ZohoTransientError(
                        f'Zoho update returned false for {canonical} #{order_id}'
                    )

                update_record_fields(
                    matched_phone_lead.zoho_module,
                    matched_phone_lead.zoho_lead_id,
                    {EXTERNAL_ORDER_KEY_FIELD: get_order_external_key(order)},
                )
                if hasattr(order, 'file_attachments'):
                    sync_order_attachments(
                        order,
                        matched_phone_lead.zoho_module,
                        matched_phone_lead.zoho_lead_id,
                    )
                order.zoho_synced = True
                order.save(update_fields=['zoho_synced'])
                job.zoho_module = matched_phone_lead.zoho_module
                job.zoho_record_id = matched_phone_lead.zoho_lead_id
            else:
                if canonical in ('quote', 'pre-check', 'fingerprinting', 'application'):
                    result = sync_func(order)
                else:
                    result = sync_func(
                        order,
                        tracking_id=tracking_id or job.tracking_id or None,
                    )
                if not result:
                    raise ZohoTransientError(
                        f'Zoho sync returned false for {canonical} #{order_id}'
                    )
                job.refresh_from_db(fields=['zoho_module', 'zoho_record_id'])

            job.status = ZohoSyncJob.STATUS_SYNCED
            job.synced_at = timezone.now()
            job.last_error = ''
            job.save(
                update_fields=[
                    'status',
                    'zoho_module',
                    'zoho_record_id',
                    'synced_at',
                    'last_error',
                    'updated_at',
                ]
            )
            logger.info('[Celery] Synced %s #%s to Zoho', canonical, order_id)
    except model_class.DoesNotExist:
        ZohoSyncJob.objects.filter(
            order_type=canonical,
            order_id=order_id,
        ).update(
            status=ZohoSyncJob.STATUS_FAILED,
            last_error='Order no longer exists',
            updated_at=timezone.now(),
        )
        logger.warning('[Celery] Order %s #%s no longer exists', canonical, order_id)
    except ZohoPermanentError as exc:
        ZohoSyncJob.objects.filter(
            order_type=canonical,
            order_id=order_id,
        ).update(
            status=ZohoSyncJob.STATUS_FAILED,
            attempts=F('attempts') + 1,
            last_error=str(exc)[:2000],
            last_attempt_at=timezone.now(),
            updated_at=timezone.now(),
        )
        logger.exception(
            '[Celery] Permanent Zoho failure for %s #%s',
            canonical,
            order_id,
        )
        raise
    except ZohoTransientError as exc:
        ZohoSyncJob.objects.filter(
            order_type=canonical,
            order_id=order_id,
        ).update(
            status=ZohoSyncJob.STATUS_PENDING,
            attempts=F('attempts') + 1,
            last_error=str(exc)[:2000],
            last_attempt_at=timezone.now(),
            updated_at=timezone.now(),
        )
        logger.warning(
            '[Celery] Temporary Zoho failure for %s #%s: %s',
            canonical,
            order_id,
            exc,
        )
        raise
    except Exception as exc:
        ZohoSyncJob.objects.filter(
            order_type=canonical,
            order_id=order_id,
        ).update(
            status=ZohoSyncJob.STATUS_PENDING,
            attempts=F('attempts') + 1,
            last_error=str(exc)[:2000],
            last_attempt_at=timezone.now(),
            updated_at=timezone.now(),
        )
        logger.exception(
            '[Celery] Unexpected Zoho failure for %s #%s',
            canonical,
            order_id,
        )
        raise ZohoTransientError(str(exc)) from exc


@shared_task
def reconcile_pending_zoho_syncs():
    """Republish durable pending intents without scanning ambiguous legacy rows."""
    stale_running_before = timezone.now() - datetime.timedelta(minutes=15)
    ZohoSyncJob.objects.filter(
        status=ZohoSyncJob.STATUS_RUNNING,
        updated_at__lt=stale_running_before,
    ).update(
        status=ZohoSyncJob.STATUS_PENDING,
        last_error='Recovered stale running job',
        updated_at=timezone.now(),
    )

    queued = 0
    pending_before = timezone.now() - datetime.timedelta(minutes=2)
    jobs = ZohoSyncJob.objects.filter(
        status=ZohoSyncJob.STATUS_PENDING,
        created_at__lte=pending_before,
    ).order_by('created_at')
    for job in jobs.iterator():
        if job.order_type not in ORDER_TYPE_MAP:
            ZohoSyncJob.objects.filter(pk=job.pk).update(
                status=ZohoSyncJob.STATUS_FAILED,
                last_error=f'Unknown order type: {job.order_type}',
                updated_at=timezone.now(),
            )
            continue
        try:
            sync_order_to_zoho_task.delay(
                job.order_id,
                job.order_type,
                tracking_id=job.tracking_id or None,
            )
            queued += 1
        except Exception as exc:
            logger.exception(
                '[Celery] Failed to republish Zoho sync for %s #%s',
                job.order_type,
                job.order_id,
            )
            ZohoSyncJob.objects.filter(pk=job.pk).update(
                last_error=f'Queue publish failed: {exc}'[:2000],
                updated_at=timezone.now(),
            )

    logger.info('[Celery] Reconciliation queued %s Zoho syncs', queued)
    return queued


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


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=3,  # Start with 3 sec, then 6, 12, 24...
    retry_backoff_max=60,  # Max 60 sec between retries
    retry_kwargs={'max_retries': 5},
)
def send_tracking_email_task(self, tid: str, stage_code: str):
    """
    Sends HTML email with order status update.
    All emails are grouped into one thread by TID.

    NOTE: 'completed' stage email is handled by reviews app (review request).

    Retries up to 5 times with exponential backoff (3s, 6s, 12s, 24s, 48s)
    to handle Resend API rate limits (429 Too Many Requests).
    """
    from django.template.loader import render_to_string
    from django.core.mail import EmailMessage
    import logging

    logger = logging.getLogger(__name__)
    
    # Skip 'completed' stage - handled by reviews app
    if stage_code == 'completed':
        logger.info(f"Skipping tracking email for TID {tid}, stage 'completed' - handled by reviews app")
        return
    
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
    
    # Determine title and message based on stage
    if stage_code == 'created':
        title = "Order Received 📋"
        message = "Thank you for choosing DC Mobile Notary! We have received your order and will begin processing it shortly."
    elif stage_code == 'document_received':
        title = "Documents Received ✅"
        if svc == 'FBI Apostille':
            message = "We have successfully received your documents and they are now in our processing queue."
        else:
            message = "We have received your documents and will review them shortly. Our team will be in touch with you soon."
    elif stage_code == 'quote_review':
        title = "Request Under Review 📝"
        message = "Your request is being reviewed by our specialists. We will contact you shortly with pricing details and next steps."
    elif stage_code == 'notarized':
        title = "Notarization in Progress"
        message = "Your documents are currently being notarized and prepared for the next step in the process."
    elif stage_code == 'submitted':
        title = "Submission in Progress"
        message = "Your documents are under Review for Federal authentication."
    elif stage_code == 'processed_dos':
        title = "Processing at U.S. DoS"
        message = "Your documents are currently being processed by the U.S. Department of State."
    elif stage_code == 'processed_state':
        title = "Processing at State Authority"
        message = "Your documents are being processed by the state authority."
    elif stage_code == 'state_authenticated':
        title = "State Authentication in Progress"
        message = "Your documents are currently undergoing state authentication."
    elif stage_code == 'federal_authenticated':
        title = "Federal Authentication in Progress"
        message = "Your documents are currently undergoing federal authentication."
    elif stage_code == 'embassy_legalized':
        title = "Embassy Legalization in Progress"
        message = "Your documents are being legalized by the embassy/consulate."
    elif stage_code == 'in_translation':
        title = "Translation in Progress"
        message = "Your documents are currently being translated by our certified translators."
    elif stage_code == 'translated':
        title = "Translation Review in Progress"
        message = "Your translation is complete and is now undergoing quality review."
    elif stage_code == 'quality_approved':
        title = "Quality Review in Progress"
        message = "Your translation is currently undergoing a rigorous quality assurance review to ensure accuracy."
    elif stage_code == 'in_progress':
        title = "Order in Progress"
        message = "Your order is being processed. We will notify you once it is ready for delivery."
    elif stage_code == 'delivered':
        title = "Order Out for Delivery"
        message = "Your order is on its way to you! We hope you're satisfied with our service."
    elif stage_code == 'completed':
        title = "Order Completed"
        message = "Your order has been successfully completed. Thank you for choosing our services!"
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
        retry_num = self.request.retries
        max_retries = self.max_retries
        logger.warning(f"⚠️ Failed to send tracking email for TID {tid} (attempt {retry_num + 1}/{max_retries + 1}): {e}")
        raise  # Re-raise to trigger Celery retry
