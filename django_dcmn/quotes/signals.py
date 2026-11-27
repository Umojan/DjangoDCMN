"""S1 shadow receiver on `orders.QuoteRequest` (DESIGN §13.2 S1); the legacy receiver is untouched.

Imported by `QuotesConfig.ready()` only when QUOTE_V3_SHADOW_LEGACY is on, so with the flag off nothing is
connected. The task runs 90 s after commit, once the legacy sync has created the GAQL record and its copy.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from orders.models import QuoteRequest

logger = logging.getLogger(__name__)

DISPATCH_UID = 'quotes_shadow_legacy_quote'
SHADOW_DELAY_S = 90


@receiver(post_save, sender=QuoteRequest, dispatch_uid=DISPATCH_UID)
def shadow_legacy_quote(sender, instance, created, **kwargs):
    if not created:
        return
    from .tasks import shadow_map_legacy_quote

    pk = instance.pk

    def publish():
        try:      # never let the shadow break the legacy quote request
            shadow_map_legacy_quote.apply_async((pk,), countdown=SHADOW_DELAY_S)
        except Exception:
            logger.exception('quote_shadow_publish_failed quote_request=%s', pk)

    transaction.on_commit(publish)
