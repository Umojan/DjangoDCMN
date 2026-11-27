"""The `Get_A_Quote_Leads` record of a v3 quote (DESIGN §5.3).

`build_gaql_record` maps a submission to the GAQL create payload; `guarded_record` passes it through
`crm.schema.guard`, which drops what Zoho would not accept. Only `External_Order_Key`, `Name` and the
`Web Quote Request` bucket block the create: a copying bucket value is never written (DESIGN D3, C1).
"""

from crm import schema
from crm.schema import GuardResult

from . import contract as c
from . import flags
from .models import GAQL_MODULE, QuoteLine, QuoteSubmission
from .render import comments_text, line_price_text, line_summary, lines_text, source_page

GAQL_BLOCKING = ('External_Order_Key', 'Name', 'GET_A_QUOTE_LEADS')
WEB_QUOTE_REQUEST = 'Web Quote Request'
# The six bucket values the native copy rules fire on (DESIGN §6.5): never written on a v3 record.
COPYING_VALUES = frozenset({
    'Apostille Service', 'Notary Service', 'Translation Service', 'Embassy Legalization Services',
    'I-9 Employment Verification Services', 'Fingerprinting Services',
})
SUBFORM_FIELD = 'Quote_Lines'

NAME_MAX = 120
PHONE_MAX = 30
EMAIL_MAX = 100
TEXT_MAX = 255
LANGUAGE = {'en': 'English', 'es': 'Spanish'}

# Document_Type of the primary line (text 255), by service and answer.
EMBASSY_DOC_TYPES = {
    'personal': 'Personal documents', 'corporate': 'Company documents',
    'both': 'Personal and company documents', 'unsure': 'Not sure',
}
NOTARY_SERVICES = ('mobile-notary', 'ron', 'office-notary')


def record_key(quote: QuoteSubmission) -> str:
    """`dcmn:qr:{pk}`: never collides with the legacy `dcmn:quote:{id}` keys (DESIGN D7)."""
    return f'dcmn:qr:{quote.pk}'


def record_name(quote: QuoteSubmission) -> str:
    """`Q-482913 · Maria Fernández`, cut to fit 120 (the guard never cuts a Name)."""
    prefix = f'{quote.reference} · '
    return prefix + quote.full_name[:NAME_MAX - len(prefix)]


def primary_line(lines: list[QuoteLine]) -> QuoteLine | None:
    return next((line for line in lines if line.is_primary), lines[0] if lines else None)


def build_gaql_record(quote: QuoteSubmission, *, subform: bool = False) -> dict:
    """The §5.3 create payload, before the schema guard; `subform` adds the `Quote_Lines` rows."""
    lines = list(quote.lines.order_by('position'))
    primary = primary_line(lines)
    targetable = [line for line in lines if line.service_key not in c.NO_TARGET_SERVICES]

    record = {
        'Name': record_name(quote),
        'External_Order_Key': record_key(quote),
        'Quote_Reference': quote.reference,
        'GET_A_QUOTE_LEADS': WEB_QUOTE_REQUEST,
        'Quote_Status': 'New',     # written once, here; managers own it afterwards (DESIGN C7)
        'Service_Progress': 'Not started' if targetable else 'Nothing to create',
        'Lines_Pending': len(targetable),
        'Primary_Service': c.SERVICE_DISPLAY.get(quote.primary_service or (primary and primary.service_key)),
        'Services_Requested': [c.SERVICE_DISPLAY[line.service_key] for line in lines],
        'Services_Created': [],
        'Quote_Lines_Text': lines_text(quote),
        'Client_Name': quote.full_name,
        'Client_Phone': quote.phone_e164 or quote.phone_raw[:PHONE_MAX],
    }
    if quote.email and len(quote.email) <= EMAIL_MAX:
        record['Client_Email'] = quote.email
    if quote.zoho_contact_id:
        record['Name_of_Client'] = {'id': quote.zoho_contact_id}
    if quote.zoho_attribution_id:
        record['Attribution_Record'] = {'id': quote.zoho_attribution_id}
    if primary is not None:
        record.update(_primary_fields(primary))
    if quote.when in c.WHEN_LABELS:
        record['Urgency'] = c.WHEN_LABELS[quote.when]
    if quote.deadline:
        record['Date_Time'] = f'By {quote.deadline.isoformat()}'
    if quote.effective_channel in c.CHANNEL_DISPLAY:
        record['Contact_Preference'] = c.CHANNEL_DISPLAY[quote.effective_channel]
    record['WhatsApp_Opt_In'] = quote.whatsapp_opt_in
    record['SMS_Opt_In'] = quote.sms_opt_in
    if quote.consent_text_version:
        record['Consent_Version'] = quote.consent_text_version
    record['Lead_Language'] = LANGUAGE.get(quote.locale, 'English')
    page = source_page(quote)
    if page:
        record['Quote_Source_Page'] = page[:TEXT_MAX]
    record['Client_Comments'] = comments_text(quote)
    owner = flags.quote_owner_id()
    if owner:
        record['Owner'] = {'id': owner}
    if subform:
        record[SUBFORM_FIELD] = [_subform_row(quote, line) for line in lines]
    return {name: value for name, value in record.items() if value is not None}


def _primary_fields(line: QuoteLine) -> dict:
    """Number_Of_Documents, Document_Type, Country_of_Use (only if answered) and the mobile-notary ZIP."""
    answers = line.answers or {}
    fields: dict = {}
    if line.service_key in ('apostille', 'embassy') and isinstance(answers.get('count'), int):
        fields['Number_Of_Documents'] = answers['count']   # never a default 1 [04 R4]
    doc = _document_type(line.service_key, answers)
    if doc:
        fields['Document_Type'] = doc[:TEXT_MAX]
    if line.service_key in ('apostille', 'embassy') and answers.get('country'):
        fields['Country_of_Use'] = c.country_name(answers['country'])[:TEXT_MAX]
    if line.service_key == 'mobile-notary' and answers.get('zip'):
        fields['Client_Address_Location'] = f'ZIP {answers["zip"]}'   # never a street
    return fields


def _document_type(service: str, answers: dict) -> str:
    if service == 'apostille' and answers.get('doc'):
        return c.APOSTILLE_DOC_LABELS.get(answers['doc'], '')
    if service == 'embassy' and answers.get('kind'):
        return EMBASSY_DOC_TYPES.get(answers['kind'], '')
    if service in NOTARY_SERVICES and answers.get('doc'):
        return c.ANSWER_LABELS[(service, 'doc')].get(answers['doc'], '')
    return ''


def _subform_row(quote: QuoteSubmission, line: QuoteLine) -> dict:
    """One `Quote_Lines` row at create (phase 2, QUOTE_V3_SUBFORM=auto)."""
    no_target = line.service_key in c.NO_TARGET_SERVICES
    return {
        'Service': c.SERVICE_DISPLAY[line.service_key],
        'Service_Key': line.service_key,
        'Line_Ref': f'{quote.reference}/{line.position}',
        'Details': line_summary(line)[:2000],
        'Price_Shown': line_price_text(line)[:TEXT_MAX],
        'Line_Status': 'No pipeline' if no_target else 'Requested',
    }


def use_subform() -> bool:
    """Subform rows only with QUOTE_V3_SUBFORM=auto and the field present in the metadata (DESIGN §5.3)."""
    return flags.subform_mode() == 'auto' and SUBFORM_FIELD in (schema.get_fields(GAQL_MODULE) or {})


def guarded_record(quote: QuoteSubmission, *, subform: bool | None = None) -> GuardResult:
    """The payload as it may be written: off-list values dropped, text cut, blocking fields reported."""
    record = build_gaql_record(quote, subform=use_subform() if subform is None else subform)
    return schema.guard(GAQL_MODULE, record, GAQL_BLOCKING)


def dropped_text(result: GuardResult) -> str:
    """`Not recorded in Zoho fields: Urgency 'As soon as possible', …` for the receipt Note; '' when none."""
    if not result.dropped:
        return ''
    items = ', '.join(f"{name} '{value}'" for name, value in result.dropped)
    return f'Not recorded in Zoho fields: {items}'
