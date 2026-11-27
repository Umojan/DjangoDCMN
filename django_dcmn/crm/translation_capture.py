"""Translation checkbox on the legacy service forms (DESIGN §10.1, §10.2); no `orders/` change.

Thin subclasses of the legacy order views, routed in `django_dcmn/urls.py` above the orders include with the
same paths and names. The legacy view runs unchanged and its response object is returned as is. On a 201
with an `order_id`, a ticked `need_translation` becomes one `ServiceSignal` (`waiting_source`), which
`crm.tasks.apply_translation_signals` writes to Zoho once the order has synced. A ticked box on a form whose
order type is not in TRANSLATION_CAPTURE_TYPES is counted per day and order type for the daily digest.
"""

import logging
from collections.abc import Mapping
from datetime import date, timedelta

from django.core.cache import cache
from django.utils import timezone

from orders.views.orders import (
    CreateApostilleOrderView,
    CreateEmbassyOrderView,
    CreateFbiOrderView,
    CreateFingerprintingView,
    CreateMarriageOrderView,
    CreatePreCheckView,
)
from quotes import contract as c
from quotes.render import ET

from . import flags
from .models import ServiceSignal

logger = logging.getLogger(__name__)

KIND = 'translation_requested'
SERVICE_KEY = 'translation'
SIGNAL_TTL = timedelta(days=30)
LANGUAGE_MAX = 60
TICKED = frozenset({'yes', 'on', 'true', '1'})
# (form field, signal data key); the raw value is kept next to it as `{key}_raw` (purged after 90 days).
LANGUAGE_FIELDS = (('translation_source_language', 'lang_from'), ('translation_target_language', 'lang_to'))

IGNORED_KEY = 'crm:translation_capture_ignored:{day}:{order_type}'
IGNORED_TTL_S = 8 * 24 * 3600


# --- Parsing ----------------------------------------------------------------------------------------

def parse_translation(data: Mapping) -> dict | None:
    """The request's translation answer, or None unless `need_translation` is ticked (DESIGN §10.1).

    Each language is normalised to its English name when it is an EN name, ES name or `languages.json` slug,
    else kept as typed (≤60); the raw value is kept as well.
    """
    if not _ticked(data.get('need_translation')):
        return None
    parsed = {}
    for field, key in LANGUAGE_FIELDS:
        raw = _text(data.get(field))
        parsed[key] = english_language(raw)
        parsed[f'{key}_raw'] = raw
    return parsed


def _ticked(value) -> bool:
    return value is not None and str(value).strip().lower() in TICKED


def _text(value) -> str:
    return ' '.join(value.split())[:LANGUAGE_MAX] if isinstance(value, str) else ''


def english_language(value: str) -> str:
    slug = c.language_lookup().get(c.fold(value)) if value else None
    return c.language_name(slug) if slug else value


# --- Capture ----------------------------------------------------------------------------------------

def capture(data: Mapping, order_type: str, order_id: int) -> ServiceSignal | None:
    """A ticked checkbox → one `waiting_source` signal per order; an order type that is off is only counted."""
    parsed = parse_translation(data)
    if parsed is None:
        return None
    if order_type not in flags.translation_capture_types():
        count_ignored(order_type)
        logger.info('translation_capture_ignored order_type=%s order_id=%s', order_type, order_id)
        return None
    source_key = f'form:{order_type}:{order_id}'
    signal, created = ServiceSignal.objects.get_or_create(
        dedupe_key=f'{KIND}:{source_key}:{SERVICE_KEY}',
        defaults={
            'kind': KIND, 'source_kind': 'form', 'source_key': source_key, 'order_type': order_type,
            'order_id': int(order_id), 'service_key': SERVICE_KEY, 'data': parsed, 'state': 'waiting_source',
            'expires_at': timezone.now() + SIGNAL_TTL,
        },
    )
    if created:
        logger.info('translation_captured signal=%s order_type=%s order_id=%s', signal.pk, order_type, order_id)
    return signal


def count_ignored(order_type: str) -> None:
    key = IGNORED_KEY.format(day=timezone.now().astimezone(ET).date().isoformat(), order_type=order_type)
    cache.add(key, 0, IGNORED_TTL_S)
    try:
        cache.incr(key)
    except ValueError:   # expired between add and incr
        cache.set(key, 1, IGNORED_TTL_S)


def ignored_counts(day: date) -> dict[str, int]:
    """`translation_capture_ignored` per order type on one ET day (a digest line, DESIGN §12.2)."""
    keys = {order_type: IGNORED_KEY.format(day=day.isoformat(), order_type=order_type) for order_type in ORDER_TYPES}
    found = cache.get_many(list(keys.values()))
    return {order_type: int(found[key]) for order_type, key in keys.items() if found.get(key)}


# --- The views --------------------------------------------------------------------------------------

class TranslationCaptureMixin:
    """Runs the legacy view, then captures the checkbox; the legacy response object is returned unchanged."""

    order_type: str

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        body = getattr(response, 'data', None)
        if (flags.translation_capture_enabled() and response.status_code == 201
                and isinstance(body, Mapping) and 'order_id' in body):
            try:
                capture(request.data, self.order_type, body['order_id'])
            except Exception:
                logger.exception('translation_capture_failed order_type=%s', self.order_type)
        return response


class FbiOrderWithTranslation(TranslationCaptureMixin, CreateFbiOrderView):
    order_type = 'fbi'


class ApostilleOrderWithTranslation(TranslationCaptureMixin, CreateApostilleOrderView):
    order_type = 'apostille'


class EmbassyOrderWithTranslation(TranslationCaptureMixin, CreateEmbassyOrderView):
    order_type = 'embassy'


class MarriageOrderWithTranslation(TranslationCaptureMixin, CreateMarriageOrderView):
    order_type = 'marriage'


class FingerprintingWithTranslation(TranslationCaptureMixin, CreateFingerprintingView):
    order_type = 'fingerprinting'


class PreCheckWithTranslation(TranslationCaptureMixin, CreatePreCheckView):
    """The frontend put the switch on Pre-Check too (QUESTIONS V24): counted as ignored unless the type is added."""

    order_type = 'pre-check'


CAPTURE_VIEWS = (FbiOrderWithTranslation, ApostilleOrderWithTranslation, EmbassyOrderWithTranslation,
                 MarriageOrderWithTranslation, FingerprintingWithTranslation, PreCheckWithTranslation)
ORDER_TYPES = tuple(view.order_type for view in CAPTURE_VIEWS)
