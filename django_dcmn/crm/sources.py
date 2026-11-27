"""Canonical sources of engine calls (DESIGN §7.2): which record, which `source_key`, which services it requests."""

import re
from dataclasses import dataclass, field

from quotes.models import GAQL_MODULE, QuoteSubmission

from . import flags, zoho
from .facts import FORM_ORDER_LABELS, GAQL_REF_LABEL, SOURCE_FIELD_MAPS, Facts, from_form_order, from_quote, \
    from_zoho_record, is_test_name, source_url
from .models import ServiceSignal

QR_KEY = re.compile(r'^dcmn:qr:(\d+)$')
FORM_KEY = re.compile(rf'^dcmn:({"|".join(FORM_ORDER_LABELS)}):(\d+)$')

# `rel:{code}:{id}` per source module.
MODULE_CODES = {
    GAQL_MODULE: 'gaql', 'Deals': 'deal', 'Apostille_Services': 'apo', 'Embassy_Legalization': 'emb',
    'Translation_Services': 'trn', 'FINGERPRINT_SERVICES': 'fpr', 'I_9_Verification': 'i9',
    'Notary_Services': 'nty', 'Triple_Seal_Apostilles': 'tsa', 'Leads': 'lead',
}
# Legacy GAQL bucket → requested service.
BUCKET_SERVICES = {
    'Apostille Service': 'apostille',
    'Notary Service': 'notary',
    'Translation Service': 'translation',
    'Embassy Legalization Services': 'embassy',
    'I-9 Employment Verification Services': 'i9',
    'Fingerprinting Services': 'fingerprinting',
}


class SourceNotAllowed(Exception):
    """The module is not in CRM_SOURCES_ALLOWED or has no SOURCE_FIELD_MAPS row (HTTP 403)."""


class SourceNotFound(Exception):
    """Zoho has no such record (204/404)."""


@dataclass
class SourceRef:
    kind: str               # quote | zoho | form
    key: str                # qr:57 · rel:gaql:<id> · form:apostille:943
    module: str
    record_id: str
    url: str
    ref_label: str
    requested: list[str]
    record: dict | None     # the record as read by `resolve`; None when built from Django
    quote: QuoteSubmission | None = field(default=None, repr=False)

    @property
    def form_order(self) -> tuple[str, int] | None:
        if self.kind != 'form':
            return None
        _, order_type, order_id = self.key.split(':')
        return order_type, int(order_id)


def check_allowed(module: str) -> None:
    if module not in flags.sources_allowed() or module not in SOURCE_FIELD_MAPS:
        raise SourceNotAllowed(module)


def resolve(module: str, record_id: str, *, timeout: zoho.Timeout = zoho.DEFAULT_TIMEOUT) -> SourceRef:
    """Read the record with Django's own token (one GET) and canonicalise it."""
    check_allowed(module)
    record = zoho.get(module, str(record_id), timeout=timeout)
    if record is None:
        raise SourceNotFound(f'{module}/{record_id}')
    key = str(record.get(zoho.KEY_FIELD) or '')
    url = source_url(module, str(record_id)) or ''

    match = QR_KEY.match(key)
    if module == GAQL_MODULE and match:
        quote = QuoteSubmission.objects.filter(pk=int(match[1])).first()
        if quote is not None:
            return for_quote(quote, record=record)
    match = FORM_KEY.match(key)
    if match:
        order_type, order_id = match[1], int(match[2])
        source_key = f'form:{order_type}:{order_id}'
        requested = list(dict.fromkeys(
            ServiceSignal.objects.filter(source_key=source_key).order_by('created_at')
            .values_list('service_key', flat=True)))
        return SourceRef('form', source_key, module, str(record_id), url,
                         f'{FORM_ORDER_LABELS[order_type]} order #{order_id}', requested, record)
    requested = []
    if module == GAQL_MODULE and record.get('GET_A_QUOTE_LEADS') in BUCKET_SERVICES:
        requested = [BUCKET_SERVICES[record['GET_A_QUOTE_LEADS']]]
    return SourceRef('zoho', f'rel:{MODULE_CODES.get(module, module.lower())}:{record_id}', module, str(record_id),
                     url, GAQL_REF_LABEL if module == GAQL_MODULE else module.replace('_', ' '), requested, record)


def for_quote(quote: QuoteSubmission, *, record: dict | None = None) -> SourceRef:
    """The source of a v5 quote, from Django (no HTTP)."""
    requested = list(quote.lines.order_by('position').values_list('service_key', flat=True))
    return SourceRef('quote', f'qr:{quote.pk}', GAQL_MODULE, quote.zoho_record_id, quote.zoho_url, quote.reference,
                     requested, record, quote=quote)


def for_link(link, *, timeout: zoho.Timeout = zoho.DEFAULT_TIMEOUT) -> SourceRef:
    """The source of an existing ServiceLink (a v5 quote from Django, anything else re-read from Zoho)."""
    if link.source_kind == 'quote':
        quote = link.quote or QuoteSubmission.objects.get(pk=int(link.source_key.split(':')[1]))
        return for_quote(quote)
    return resolve(link.source_module, link.source_record_id, timeout=timeout)


def facts_for(source: SourceRef, service_key: str) -> Facts:
    if source.kind == 'quote':
        line = source.quote.lines.filter(service_key=service_key).first()
        return from_quote(source.quote, line)
    if source.kind == 'form':
        order_type, order_id = source.form_order
        signal = ServiceSignal.objects.filter(source_key=source.key, service_key=service_key).first()
        return from_form_order(order_type, order_id, source.record or {}, signal, module=source.module)
    return from_zoho_record(source.module, source.record or {'id': source.record_id}, key=source.key,
                            ref_label=source.ref_label)


def is_test(source: SourceRef) -> bool:
    """The client name starts with "ZZ TEST": the CRM_ENGINE_TEST_ONLY gate of the engine and the panels."""
    if source.kind == 'quote' and source.quote is not None:
        return is_test_name(source.quote.full_name)
    field = SOURCE_FIELD_MAPS.get(source.module, {}).get('client_name', '')
    return is_test_name((source.record or {}).get(field))


def is_spam(source: SourceRef) -> bool:
    """GAQL `Quote_Status = Spam` or bucket `Useless/Spam Call Leads` (DESIGN §7.8 step 4)."""
    record = source.record or {}
    if source.kind == 'quote' and source.quote is not None and source.quote.spam_status == 'spam':
        return True
    return record.get('Quote_Status') == 'Spam' or record.get('GET_A_QUOTE_LEADS') == 'Useless/Spam Call Leads'
