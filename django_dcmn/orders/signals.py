from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import (
    Application,
    ApostilleOrder,
    EmbassyLegalizationOrder,
    FbiApostilleOrder,
    FingerprintingSubmission,
    I9VerificationOrder,
    MarriageOrder,
    PhoneCallLead,
    PreCheckSubmission,
    QuoteRequest,
    TranslationOrder,
    ZohoSyncJob,
)


ORDER_TYPE_BY_SENDER = {
    FbiApostilleOrder: 'fbi',
    EmbassyLegalizationOrder: 'embassy',
    TranslationOrder: 'translation',
    ApostilleOrder: 'apostille',
    MarriageOrder: 'marriage',
    I9VerificationOrder: 'i9',
    QuoteRequest: 'quote',
    PreCheckSubmission: 'pre-check',
    FingerprintingSubmission: 'fingerprinting',
    PhoneCallLead: 'phone',
    Application: 'application',
}
PAYMENT_GATED_MODELS = (FbiApostilleOrder, MarriageOrder)


@receiver(post_save, sender=FbiApostilleOrder)
@receiver(post_save, sender=EmbassyLegalizationOrder)
@receiver(post_save, sender=TranslationOrder)
@receiver(post_save, sender=ApostilleOrder)
@receiver(post_save, sender=MarriageOrder)
@receiver(post_save, sender=I9VerificationOrder)
@receiver(post_save, sender=QuoteRequest)
@receiver(post_save, sender=PreCheckSubmission)
@receiver(post_save, sender=FingerprintingSubmission)
@receiver(post_save, sender=PhoneCallLead)
@receiver(post_save, sender=Application)
def ensure_zoho_sync_intent(sender, instance, created, raw=False, **kwargs):
    """Create outbox intent in the same transaction as the source record."""
    if raw or instance.zoho_synced:
        return

    if sender in PAYMENT_GATED_MODELS:
        if not instance.is_paid:
            return
    elif not created:
        return

    ZohoSyncJob.objects.get_or_create(
        order_type=ORDER_TYPE_BY_SENDER[sender],
        order_id=instance.id,
        defaults={'status': ZohoSyncJob.STATUS_PENDING},
    )
