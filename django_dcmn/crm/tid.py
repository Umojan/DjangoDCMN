"""Silent Tracking IDs for engine-created records (DESIGN §7.12).

The `Track` row is inserted before the create and its tid travels in the payload, so the Zoho Create-TID rule
(empty `Tracking_ID`) never fires and no client email is sent unless `ENGINE_TID_CREATED_EMAIL` says so.
"""

import logging

from django.db import IntegrityError, transaction

from orders.models import Track
from orders.tasks import send_tracking_email_task
from orders.utils import generate_tid
from quotes import flags as quote_flags
from quotes.models import QuoteSubmission

from . import flags
from .registry import REGISTRY, stage_kind

logger = logging.getLogger(__name__)

TRACK_ORIGIN = 'crm'
FIRST_STAGE = 'document_received'
CONFIRMATION_NOT_SENT = ('off', 'skipped')
MINT_ATTEMPTS = 3


def tid_service(service_key: str, source_kind: str, source_key: str) -> str | None:
    """The tracking service of a registry key; an Apostille made from a Deal is an FBI apostille."""
    spec = REGISTRY.get(service_key)
    if spec is None or not spec.tid_service:
        return None
    if spec.key == 'apostille' and stage_kind(source_kind, source_key) == 'deal':
        return 'fbi_apostille'
    return spec.tid_service


def mint_silent(module: str, service_key: str, facts, link) -> str | None:
    """The tid to send in the create payload, or None when the module's policy is not `mint_silent`.

    A retry reuses `link.tracking_id`; otherwise a `Track` row is inserted and its tid saved on the link.
    """
    if flags.tid_policy().get(module) != 'mint_silent':
        return None
    service = tid_service(service_key, link.source_kind, link.source_key)
    if service is None:
        return None
    if link.tracking_id:
        return link.tracking_id
    data = {
        'name': facts.client.get('name') or '', 'email': facts.client.get('email') or '', 'service': service,
        'current_stage': FIRST_STAGE, 'zoho_module': module, 'record_id': '', 'origin': TRACK_ORIGIN,
        'source_key': link.source_key,
    }
    for _ in range(MINT_ATTEMPTS):
        tid = generate_tid()
        try:
            with transaction.atomic():
                Track.objects.create(tid=tid, service=service, data=data)
            break
        except IntegrityError:   # a tid collision: draw another
            continue
    else:
        raise RuntimeError('could not mint a unique tracking id')
    link.tracking_id = tid
    link.save(update_fields=['tracking_id', 'updated_at'])
    logger.info('tid_minted link=%s module=%s', link.pk, module)
    return tid


def set_record_id(link) -> None:
    """After the create: the Track row points at the new Zoho record."""
    track = Track.objects.filter(tid=link.tracking_id).first() if link.tracking_id else None
    if track is None:
        return
    data = dict(track.data or {})
    if data.get('origin') == TRACK_ORIGIN and data.get('record_id') != link.target_record_id:
        data['record_id'] = link.target_record_id
        track.data = data
        track.save(update_fields=['data', 'updated_at'])


def maybe_send_created_email(link) -> bool:
    """Queue the "created" tracking email once per quote (ENGINE_TID_CREATED_EMAIL=first_per_quote).

    Only for a v5 quote source whose client confirmation was not sent or queued, never for a test quote
    (unless QUOTE_V3_TEST_EMAILS), and only for the first link of the quote that claims `created_email_tid`.
    """
    if flags.tid_created_email() != 'first_per_quote' or not link.tracking_id:
        return False
    if link.source_kind != 'quote' or link.quote_id is None:
        return False
    quote = QuoteSubmission.objects.filter(pk=link.quote_id).first()
    if quote is None or quote.confirmation_email_status not in CONFIRMATION_NOT_SENT:
        return False
    if quote.is_test and not quote_flags.v3_test_emails():
        return False
    claimed = QuoteSubmission.objects.filter(pk=quote.pk, created_email_tid='').update(
        created_email_tid=link.tracking_id)
    if not claimed:
        return False
    try:
        send_tracking_email_task.delay(link.tracking_id, 'created')
    except Exception:
        # Not queued: give the claim back, so the email is not marked as sent (the engine alerts ops).
        QuoteSubmission.objects.filter(pk=quote.pk, created_email_tid=link.tracking_id).update(created_email_tid='')
        raise
    logger.info('tid_created_email_queued link=%s quote=%s', link.pk, quote.pk)
    return True


def void_orphan_track(link) -> None:
    """A failed link must not leave a public tracking page for a record that was never made."""
    if not link.tracking_id:
        return
    track = Track.objects.filter(tid=link.tracking_id).first()
    data = (track.data or {}) if track is not None else {}
    if track is not None and (data.get('origin') != TRACK_ORIGIN or data.get('record_id')):
        return
    if track is not None:
        track.delete()
    logger.info('tid_voided link=%s', link.pk)
    link.tracking_id = ''
    link.save(update_fields=['tracking_id', 'updated_at'])
