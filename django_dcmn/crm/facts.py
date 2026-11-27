"""One normalised view of a source for every registry mapper (DESIGN §7.2).

`from_quote` reads Django (a v5 quote and one of its lines), `from_zoho_record` reads a Zoho record through
`SOURCE_FIELD_MAPS`, `from_form_order` reads the Zoho record of a legacy Django form order. None of them makes
an HTTP call: a missing contact is found or created at create time (`crm.engine`).
"""

from dataclasses import dataclass

from quotes import contract as c
from quotes import render
from quotes.models import GAQL_MODULE, QuoteLine, QuoteSubmission

from .registry import TABS
from .zoho import record_url

# Per source module: where the client and the typed facts live. NOW only Get_A_Quote_Leads; each LATER source
# module adds its row from its fields_*.json dump when its buttons are enabled (a module without one is refused).
SOURCE_FIELD_MAPS: dict[str, dict[str, str]] = {
    GAQL_MODULE: {
        'client_name': 'Client_Name', 'email': 'Client_Email', 'phone': 'Client_Phone',
        'address': 'Client_Address_Location', 'comments': 'Client_Comments', 'contact_lookup': 'Name_of_Client',
        'attribution': 'Attribution_Record', 'count': 'Number_Of_Documents', 'doc': 'Document_Type',
        'country': 'Country_of_Use',
    },
}

FORM_ORDER_LABELS = {
    'fbi': 'FBI', 'apostille': 'Apostille', 'embassy': 'Embassy', 'fingerprinting': 'Fingerprinting',
    'marriage': 'Marriage', 'i9': 'I-9', 'translation': 'Translation',
}
GAQL_REF_LABEL = 'Get A Quote record'


@dataclass
class Facts:
    client: dict            # name, email, phone_e164, phone_raw, contact_id, address
    attribution_id: str | None
    source: dict            # kind, key, module, id, url, ref_label, is_test
    quote: dict | None      # reference, pk, gaql_id, gaql_url, n_services, services_label
    line: dict | None       # service_key, answers, answers_raw, addons, addon_of, price_shown_text, price_flag,
    #                         price_text, summary_server, implied
    typed: dict


def is_test_name(name: str | None) -> bool:
    return c.fold(name or '').startswith(c.TEST_NAME_PREFIX)


def source_url(module: str, record_id: str | None) -> str | None:
    tab = TABS.get(module)
    return record_url(tab, record_id) if tab and record_id else None


# --- v5 quote ---------------------------------------------------------------------------------------

def from_quote(quote: QuoteSubmission, line: QuoteLine | None) -> Facts:
    lines = list(quote.lines.order_by('position'))
    gaql_url = quote.zoho_url or None
    client = {
        'name': quote.full_name, 'email': quote.email or None, 'phone_e164': quote.phone_e164 or None,
        'phone_raw': quote.phone_raw or None, 'contact_id': quote.zoho_contact_id or None, 'address': None,
    }
    source = {
        'kind': 'quote', 'key': f'qr:{quote.pk}', 'module': GAQL_MODULE, 'id': quote.zoho_record_id or None,
        'url': gaql_url, 'ref_label': quote.reference, 'is_test': quote.is_test,
    }
    quote_facts = {
        'reference': quote.reference, 'pk': quote.pk, 'gaql_id': quote.zoho_record_id or None, 'gaql_url': gaql_url,
        'n_services': len(lines), 'services_label': ' + '.join(c.SERVICE_DISPLAY.get(l.service_key, l.service_key)
                                                                for l in lines),
        'is_test': quote.is_test,
    }
    contact = render.contact_line(quote).removeprefix('Contact: ')
    channel, _, when = contact.partition(' · When: ')
    typed = {**_typed_from_line(line, lines), 'channel_label': channel, 'when_label': when,
             'message': quote.message or ''}
    return Facts(client=client, attribution_id=quote.zoho_attribution_id or None, source=source, quote=quote_facts,
                 line=_line_facts(line) if line else None, typed=typed)


def _line_facts(line: QuoteLine) -> dict:
    return {
        'service_key': line.service_key, 'answers': dict(line.answers or {}),
        'answers_raw': dict(line.answers_raw or {}), 'addons': list(line.addons or []), 'addon_of': line.addon_of,
        'price_shown_text': line.price_shown_text, 'price_flag': line.price_flag,
        'price_text': render.line_price_text(line), 'summary_server': line.summary_server or render.line_summary(line),
        'implied': list(line.implied_services or []),
    }


def _typed_from_line(line: QuoteLine | None, lines: list[QuoteLine]) -> dict:
    if line is None:
        return {}
    service, answers = line.service_key, line.answers or {}
    typed: dict = {}
    doc = answers.get('doc') or answers.get('doc_from_apostille')
    if doc:
        typed['doc_kind'] = doc
        typed['doc_label'] = c.ANSWER_LABELS.get((service, 'doc'), c.APOSTILLE_DOC_LABELS).get(doc, doc)
    if answers.get('country'):
        typed['country_name'] = c.country_name(answers['country'])
    for key in ('state', 'count', 'people', 'where', 'zip', 'office'):
        if answers.get(key) is not None:
            typed[key] = answers[key]
    if service == 'embassy' and answers.get('kind'):
        typed['embassy_kind'] = answers['kind']
    if service in ('fbi', 'ron') and answers.get('apostille'):
        typed['apostille_needed'] = answers['apostille']
    if service == 'mobile-notary':
        if answers.get('when'):
            typed['notary_when'] = answers['when']
        if not answers.get('zip') and (line.answers_raw or {}).get('zip'):
            typed['zip_raw'] = str(line.answers_raw['zip'])

    # Languages: the translation line itself, or the translation add-on of an apostille / embassy line.
    translation = line if service == 'translation' else None
    if service in c.ADDON_PARENTS and 'translation' in (line.addons or []):
        typed['translation_addon'] = True
        translation = next((l for l in lines if l.service_key == 'translation' and l.addon_of == service), None) \
            or next((l for l in lines if l.service_key == 'translation'), None)
    if translation is not None:
        t_answers = translation.answers or {}
        if t_answers.get('from'):
            typed['lang_from'] = c.language_name(t_answers['from'])
        if t_answers.get('to'):
            typed['lang_to'] = c.language_name(t_answers['to'])
        if service == 'translation':
            if t_answers.get('kind'):
                typed['translation_kind'] = t_answers['kind']
            if t_answers.get('pages') is not None:
                typed['pages'] = t_answers['pages']
    return typed


# --- Zoho records ------------------------------------------------------------------------------------

def _lookup_id(value) -> str | None:
    if isinstance(value, dict):
        return str(value['id']) if value.get('id') else None
    return str(value) if value else None


def _client_from_record(field_map: dict[str, str], record: dict) -> dict:
    def text(name: str) -> str | None:
        value = record.get(field_map.get(name, ''))
        return ' '.join(str(value).split()) if value not in (None, '') else None

    phone = text('phone')
    return {
        'name': text('client_name'), 'email': text('email'), 'phone_e164': None, 'phone_raw': phone,
        'contact_id': _lookup_id(record.get(field_map.get('contact_lookup', ''))), 'address': text('address'),
    }


def _typed_from_record(field_map: dict[str, str], record: dict) -> dict:
    typed: dict = {}
    doc = record.get(field_map.get('doc', ''))
    if doc:
        typed['doc_label'] = str(doc)
    country = record.get(field_map.get('country', ''))
    if country:
        typed['country_name'] = str(country)
    count = record.get(field_map.get('count', ''))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        typed['count'] = count
    comments = record.get(field_map.get('comments', ''))
    if comments:
        typed['message'] = str(comments)   # the source's own comments: the "Message:" line, cut first
    return typed


def from_zoho_record(module: str, record: dict, *, key: str | None = None, ref_label: str = GAQL_REF_LABEL) -> Facts:
    """Facts of a Zoho source record; raises KeyError for a module without a SOURCE_FIELD_MAPS row."""
    field_map = SOURCE_FIELD_MAPS[module]
    record_id = str(record.get('id') or '')
    client = _client_from_record(field_map, record)
    source = {
        'kind': 'zoho', 'key': key or f'rel:gaql:{record_id}', 'module': module, 'id': record_id or None,
        'url': source_url(module, record_id), 'ref_label': ref_label, 'is_test': is_test_name(client['name']),
    }
    return Facts(client=client, attribution_id=_lookup_id(record.get(field_map.get('attribution', ''))),
                 source=source, quote=None, line=None, typed=_typed_from_record(field_map, record))


def from_form_order(order_type: str, order_id: int, record: dict, signal=None, *, module: str | None = None) -> Facts:
    """Facts of a legacy form order, read from its Zoho record (keyed `dcmn:{order_type}:{id}`) and its signal."""
    module = module or str(record.get('$module') or '')
    facts = from_zoho_record(module, record, key=f'form:{order_type}:{order_id}',
                             ref_label=f'{FORM_ORDER_LABELS.get(order_type, order_type.title())} order #{order_id}')
    facts.source['kind'] = 'form'
    data = (signal.data if signal is not None else None) or {}
    for name in ('lang_from', 'lang_to'):
        if data.get(name):
            facts.typed[name] = str(data[name])
    return facts
