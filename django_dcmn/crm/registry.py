"""What each related service creates in Zoho (DESIGN §7.4): module, stage, field map, constants, TID, intake.

`build` turns `Facts` into a create payload (before the schema guard); `validate` checks the registry against
live field metadata. Every value written here is an exact display value verified in the `fields_*.json` dumps;
`crm/tests/test_registry.py` re-checks them against the fixtures.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Mapping

from django.utils import timezone

from quotes import contract as c
from quotes.render import ET

from . import flags, schema
from .zoho import KEY_FIELD, record_url

if TYPE_CHECKING:
    from .facts import Facts

NAME_MAX = 120
COMMENTS_DEFAULT_MAX = 2000
TEST_PREFIX = 'ZZ TEST · '
ELLIPSIS = '…'

NO_TARGET = frozenset({'retrieval', 'other'})
NOTARY_KEYS = ('mobile-notary', 'office-notary', 'ron')
ALIASES: dict[str, tuple[str, ...]] = {'notary': NOTARY_KEYS}

# Derived from the live stage lists of fields_*.json; must match none of the intake values (test_registry).
CLOSED_STAGE_PATTERN = re.compile(r'(?i)lost|cancel|reject|complet|useles|no review|send review|delivered|shipping')
AUTOMATED_MARK = 'AUTOMATED - BY CRM'

# The workflow rules that copy a GAQL record into a service module [F1 §1.1].
COPY_RULE_IDS: frozenset[str] = frozenset({
    '6686316000002342517',   # Apostille
    '6686316000002478447',   # Notary
    '6686316000002351021',   # Translation
    '6686316000019446013',   # Fingerprint
    '6686316000002342272',   # I-9
    '6686316000002342740',   # Embassy
})

# Zoho web tab of each module, for record URLs (A10).
TABS: dict[str, str] = {
    'Apostille_Services': 'CustomModule2',
    'Embassy_Legalization': 'CustomModule1',
    'Translation_Services': 'CustomModule3',
    'FINGERPRINT_SERVICES': 'CustomModule10',
    'Notary_Services': 'CustomModule6',
    'I_9_Verification': 'CustomModule5',
    'Triple_Seal_Apostilles': 'CustomModule4',
    'Deals': 'Potentials',
    'Get_A_Quote_Leads': 'CustomModule7',
}
MODULE_LABELS: dict[str, str] = {
    'Apostille_Services': 'Apostille Services',
    'Embassy_Legalization': 'Embassy Legalization',
    'Translation_Services': 'Translation Services',
    'FINGERPRINT_SERVICES': 'Fingerprint Services',
    'Notary_Services': 'Notary Services',
    'I_9_Verification': 'I-9 Verification',
    'Triple_Seal_Apostilles': 'Triple Seal Apostilles',
    'Deals': 'Deals',
}

# Field types accepted by `validate` (Zoho data_type values).
TEXT, TEXTAREA, EMAIL, PHONE, PICKLIST, LOOKUP, WEBSITE, BOOLEAN, INTEGER, DATE, CURRENCY = (
    ('text',), ('textarea',), ('email',), ('phone', 'text'), ('picklist',), ('lookup',), ('website',),
    ('boolean',), ('integer',), ('date',), ('currency', 'double', 'decimal'),
)


class UnknownService(ValueError):
    """A service key the registry does not know (HTTP 422)."""


@dataclass(frozen=True)
class ClientFields:
    name: str | None
    emails: tuple[str, ...]
    phone: str | None
    address: str | None
    comments: str


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    label: str
    module: str
    tab: str
    stage_field: str
    stage_by_source: Mapping[str, str]   # quote|gaql|deal|form|service, or form:<order_type>
    client: ClientFields
    contact_lookup: str
    constants: Mapping[str, Any]         # a callable value is evaluated at build time
    typed: Callable[['Facts'], dict]
    tid_service: str | None
    intake: bool
    blocking: tuple[str, ...]
    copy_label: str | None               # bare Name of the workflow copy
    typed_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # api_name → accepted types
    typed_values: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # picklist values `typed` may write
    notes: Callable[['Facts'], list[str]] = lambda facts: []                     # extra comment lines
    name_field: str = 'Name'
    quote_request: bool = True
    attribution: bool = True
    search_by: str = 'email'             # how adoption finds this client's records: email | phone

    @property
    def module_label(self) -> str:
        return MODULE_LABELS.get(self.module, self.module.replace('_', ' '))

    def url(self, record_id: str) -> str:
        return record_url(self.tab, record_id)

    def stages(self) -> list[str]:
        values = list(dict.fromkeys(self.stage_by_source.values()))
        if self.key == 'apostille' and flags.apostille_fbi_stage():
            values.append(flags.apostille_fbi_stage())
        return values

    def intake_stage(self) -> str:
        return self.stage_by_source['quote']


# --- Typed mappings (each enum value: a Zoho value, an explicit omit, or a comment line) -----------

OMIT = None   # an explicit "not written to a field" (the value then appears in the comments block only)

SERVICE_AREAS = {'DC': 'DC (District of Columbia)', 'MD': 'MD (Maryland)', 'VA': 'VA (Virginia'}  # sic
OUTSIDE_DMV = 'Outside DMV Area service'
EMBASSY_DOC_TYPES = {'personal': 'Personal documents', 'corporate': 'Company documents',
                     'both': 'Personal and company documents', 'unsure': 'Not sure'}
TRANSLATION_TYPES = {'certified_digital': 'Digital Certified', 'notarized_wet': 'Certified Notarized',
                     'traduccion_jurada': OMIT, 'unsure': OMIT}
YES_NO = {'yes': 'YES', 'no': 'NO', 'unsure': OMIT}
FBI_APPOINTMENT = {'dc': 'Office', 'arlington': 'Office', 'mobile': 'Mobile', 'unsure': OMIT}
FBI_OFFICES = {'dc': 'Office: Washington, DC', 'arlington': 'Office: Arlington, VA'}
FINGERPRINT_TYPES = {'livescan': 'FBI Live Scan', 'fd258': 'FD-258 Card', 'atf': 'ATF', 'fdle': 'FDLE',
                     'unsure': OMIT}
FINGERPRINT_APPOINTMENT = {'office': 'Office', 'mobile': 'Mobile', 'unsure': OMIT}
NOTARY_WHEN = {'today': 'today', 'tomorrow': 'tomorrow', 'this_week': 'this week', 'flexible': 'flexible'}
OFFICES = {'dc': 'Washington, DC', 'arlington': 'Arlington, VA', 'ny': 'New York, NY', 'any': 'any office'}
TRANSLATION_REQUIRED_YES = 'Yes -Translate'   # the Deals display value, copied to Apostille and Embassy (§6.3)
DEAL_LANGUAGES = ('Portuguese', 'Spanish', 'Arabic', 'Chinese', 'French')
WILL_TRUST_ONLINE = "wills/trusts can't be notarized online — call client"
NO_CONTACT_DETAILS = 'No contact details on the source record'
SOURCE_ORIGINS = {'quote': 'web quote', 'zoho': 'Zoho record', 'form': 'web form order'}
ZIP_RAW_MAX = 10

# (service, answer) → value → how it is rendered: ('field', api_name, value) | ('omit', comment) | ('comment', text).
# Answers whose values have no Zoho field appear in the comments block through the line summary.
SUMMARY = ('comment', 'line summary in the comments block')


def _enum_map(api_name: str, mapping: Mapping[str, str | None], omitted: str) -> dict[str, tuple]:
    return {value: ('field', api_name, target) if target is not OMIT else ('omit', omitted)
            for value, target in mapping.items()}


ENUM_RENDERING: dict[tuple[str, str], dict[str, tuple]] = {
    ('apostille', 'doc'): {v: ('field', 'Document_Type', label) for v, label in c.APOSTILLE_DOC_LABELS.items()},
    ('apostille', 'fbi_pdf'): {v: SUMMARY for v in ('yes', 'printout', 'not_yet')},
    ('embassy', 'kind'): {v: ('field', 'Document_Type', label) for v, label in EMBASSY_DOC_TYPES.items()},
    ('embassy', 'doc_from_apostille'): {v: SUMMARY for v in c.APOSTILLE_DOCS},
    ('translation', 'kind'): _enum_map('Translation_Type', TRANSLATION_TYPES, 'Translation type comment line'),
    ('fbi', 'apostille'): _enum_map('Apostille_Needed', YES_NO, 'apostille needed: not sure (summary)'),
    ('fbi', 'where'): _enum_map('Appointment_Type', FBI_APPOINTMENT, 'where: not sure (summary)'),
    ('fingerprinting', 'type'): _enum_map('Fingerprint_Service_Type', FINGERPRINT_TYPES, 'type: not sure (summary)'),
    ('fingerprinting', 'where'): _enum_map('Appointment_Type', FINGERPRINT_APPOINTMENT, 'where: not sure (summary)'),
    ('mobile-notary', 'doc'): {v: ('field', 'Document_Type', label)
                               for v, label in c.ANSWER_LABELS[('mobile-notary', 'doc')].items()},
    ('mobile-notary', 'when'): {v: ('comment', f'When: {text}') for v, text in NOTARY_WHEN.items()},
    ('ron', 'doc'): {v: ('field', 'Document_Type', label) for v, label in c.ANSWER_LABELS[('ron', 'doc')].items()},
    ('ron', 'apostille'): {v: SUMMARY for v in c.YES_NO_UNSURE},
    ('office-notary', 'doc'): {v: ('field', 'Document_Type', label)
                               for v, label in c.ANSWER_LABELS[('office-notary', 'doc')].items()},
    ('office-notary', 'office'): {v: ('comment', f'Preferred office: {text}') for v, text in OFFICES.items()},
}


def enum_rendering(service: str, answer: str, value: str) -> tuple | None:
    """How one enum answer value reaches Zoho; None means it has no mapping (a contract gap)."""
    return ENUM_RENDERING.get((service, answer), {}).get(value)


def _answers(facts: 'Facts') -> dict:
    return (facts.line or {}).get('answers') or {}


def _translation_addon(facts: 'Facts') -> dict:
    """Translation_Required + languages when the client asked for a translation add-on (§6.3)."""
    if not facts.typed.get('translation_addon'):
        return {}
    fields = {'Translation_Required': TRANSLATION_REQUIRED_YES}
    if facts.typed.get('lang_from'):
        fields['Translation_Source_Language'] = facts.typed['lang_from']
    if facts.typed.get('lang_to'):
        fields['Translation_Target_Language'] = facts.typed['lang_to']
    return fields


def _apostille_typed(facts: 'Facts') -> dict:
    typed = facts.typed
    fields = {}
    if typed.get('doc_label'):
        fields['Document_Type'] = typed['doc_label']
    if typed.get('country_name'):
        fields['Country_of_Use'] = typed['country_name']
    if typed.get('count'):
        fields['Number_of_copies'] = str(typed['count'])
    state = typed.get('state')
    if state and state != c.STATE_UNSURE:
        fields['Service_Area'] = SERVICE_AREAS.get(state, OUTSIDE_DMV)
    return {**fields, **_translation_addon(facts)}


def _embassy_typed(facts: 'Facts') -> dict:
    typed = facts.typed
    fields = {}
    if typed.get('country_name'):
        fields['Country_of_Legalization'] = typed['country_name']
    kind = typed.get('embassy_kind')
    if kind in EMBASSY_DOC_TYPES:
        fields['Document_Type'] = EMBASSY_DOC_TYPES[kind]
    elif typed.get('doc_label'):
        fields['Document_Type'] = typed['doc_label']
    return {**fields, **_translation_addon(facts)}


def _languages(facts: 'Facts') -> str:
    source, target = facts.typed.get('lang_from'), facts.typed.get('lang_to')
    return f'{source or "?"} → {target or "?"}' if source or target else ''


def _translation_typed(facts: 'Facts') -> dict:
    fields = {}
    languages = _languages(facts)
    if languages:
        fields['Languages'] = languages
    kind = TRANSLATION_TYPES.get(facts.typed.get('translation_kind'))
    if kind:
        fields['Translation_Type'] = kind
    if facts.typed.get('pages'):
        fields['Pages'] = int(facts.typed['pages'])
    return fields


def _translation_notes(facts: 'Facts') -> list[str]:
    kind = facts.typed.get('translation_kind')
    if kind in TRANSLATION_TYPES and TRANSLATION_TYPES[kind] is OMIT:
        label = c.ANSWER_LABELS[('translation', 'kind')][kind]
        return [f'Translation type: {label} (no Zoho option)']
    return []


def _fbi_typed(facts: 'Facts') -> dict:
    answers, fields = _answers(facts), {}
    if YES_NO.get(answers.get('apostille')):
        fields['Apostille_Needed'] = YES_NO[answers['apostille']]
    if FBI_APPOINTMENT.get(answers.get('where')):
        fields['Appointment_Type'] = FBI_APPOINTMENT[answers['where']]
    if facts.typed.get('people'):
        fields['Number_of_Cards'] = int(facts.typed['people'])
    return fields


def _fbi_notes(facts: 'Facts') -> list[str]:
    office = FBI_OFFICES.get(_answers(facts).get('where'))
    return [office] if office else []


def _fingerprinting_typed(facts: 'Facts') -> dict:
    answers, fields = _answers(facts), {}
    if FINGERPRINT_TYPES.get(answers.get('type')):
        fields['Fingerprint_Service_Type'] = FINGERPRINT_TYPES[answers['type']]
    if FINGERPRINT_APPOINTMENT.get(answers.get('where')):
        fields['Appointment_Type'] = FINGERPRINT_APPOINTMENT[answers['where']]
    if facts.typed.get('people'):
        fields['Number_of_Cards'] = int(facts.typed['people'])
    return fields


def _notary_typed(facts: 'Facts') -> dict:
    return {'Document_Type': facts.typed['doc_label']} if facts.typed.get('doc_label') else {}


def _mobile_notary_typed(facts: 'Facts') -> dict:
    fields = _notary_typed(facts)
    if facts.typed.get('zip') and not facts.client.get('address'):
        fields['Client_Address_Location'] = f'ZIP {facts.typed["zip"]}'
    return fields


def _mobile_notary_notes(facts: 'Facts') -> list[str]:
    notes = []
    when = NOTARY_WHEN.get(_answers(facts).get('when'))
    if when:
        notes.append(f'When: {when}')
    raw = facts.typed.get('zip_raw')
    if raw and not facts.typed.get('zip'):
        notes.append(f'ZIP as typed (not valid): {str(raw)[:ZIP_RAW_MAX]}')
    return notes


def _office_notary_notes(facts: 'Facts') -> list[str]:
    office = OFFICES.get(_answers(facts).get('office'))
    return [f'Preferred office: {office}'] if office else []


def _ron_notes(facts: 'Facts') -> list[str]:
    return [WILL_TRUST_ONLINE] if _answers(facts).get('doc') == 'will_trust' else []


def _triple_seal_typed(facts: 'Facts') -> dict:
    summary = (facts.line or {}).get('summary_server')
    return {'Marriage_Info': summary} if summary else {}


def _deal_typed(facts: 'Facts') -> dict:
    fields = _translation_addon(facts)
    if facts.typed.get('lang_to') in DEAL_LANGUAGES:
        fields['Translated_Language'] = facts.typed['lang_to']
    return fields


def _none(facts: 'Facts') -> dict:
    return {}


def _today_plus_3() -> str:
    """today + 3 days in Eastern time, as Zoho's date format."""
    return (timezone.now().astimezone(ET).date() + timedelta(days=3)).isoformat()


# --- The registry ----------------------------------------------------------------------------------

def _blocking(stage_field: str, name_field: str = 'Name') -> tuple[str, ...]:
    return stage_field, KEY_FIELD, name_field


def _same(value: str, *kinds: str) -> dict[str, str]:
    return {kind: value for kind in (kinds or ('quote', 'gaql', 'deal', 'form', 'service'))}


TRANSLATION_ADDON_FIELDS = {'Translation_Required': PICKLIST, 'Translation_Source_Language': TEXT,
                            'Translation_Target_Language': TEXT}
NOTARY_CLIENT = ClientFields(None, ('Client_Email',), 'Client_Phone_Number', None, 'Client_Comments')
NOTARY_STAGE = _same('NOTARY LEADS FROM GET QUOTE PIPELINE')
FINGERPRINT_CLIENT = ClientFields(None, ('Email',), 'Phone', None, 'Client_Comment')
FINGERPRINT_STAGE = _same('FINGERPRINT LEADS FROM GET QUOTE PIPELINE')
TRIPLE_SEAL_STAGE = 'TRIPLE SEAL ORDERS FROM APOSTILLE'


def _notary(key: str, label: str, requesting: str | None, *, address: bool = False, typed=_notary_typed,
            notes=lambda facts: [], intake: bool = True) -> ServiceSpec:
    client = ClientFields(None, ('Client_Email',), 'Client_Phone_Number',
                          'Client_Address_Location' if address else None, 'Client_Comments')
    return ServiceSpec(
        key=key, label=label, module='Notary_Services', tab=TABS['Notary_Services'], stage_field='Notary_Stages',
        stage_by_source=NOTARY_STAGE, client=client, contact_lookup='Client_Name',
        constants={'Service_Requesting': requesting} if requesting else {}, typed=typed, tid_service=None,
        intake=intake, blocking=_blocking('Notary_Stages'), copy_label='Notary Service',
        typed_fields={'Document_Type': TEXT, 'Service_Requesting': TEXT,
                      **({'Client_Address_Location': TEXT} if address else {})},
        notes=notes,
    )


REGISTRY: dict[str, ServiceSpec] = {spec.key: spec for spec in (
    ServiceSpec(
        key='apostille', label='Apostille', module='Apostille_Services', tab=TABS['Apostille_Services'],
        stage_field='Status',
        stage_by_source={'quote': 'APOSTILE LEADS FROM GET QUOTE PIPELINE', 'gaql': 'APOSTILE LEADS FROM GET QUOTE PIPELINE',
                         'deal': 'FBI - Federal Apostille Requests', 'form': 'Client placed the request',
                         'service': 'Client placed the request'},
        client=ClientFields('Client_Name', ('Client_Email', 'Email'), 'Phone_Number', 'Address', 'Client_Comments'),
        contact_lookup='Client_Contact', constants={}, typed=_apostille_typed, tid_service='state_apostille',
        intake=True, blocking=_blocking('Status'), copy_label='Apostille Service',
        typed_fields={'Document_Type': TEXT, 'Country_of_Use': TEXT, 'Number_of_copies': TEXT,
                      'Service_Area': PICKLIST, **TRANSLATION_ADDON_FIELDS},
        typed_values={'Service_Area': (*SERVICE_AREAS.values(), OUTSIDE_DMV),
                      'Translation_Required': (TRANSLATION_REQUIRED_YES,)},
    ),
    ServiceSpec(
        key='embassy', label='Embassy Legalization', module='Embassy_Legalization', tab=TABS['Embassy_Legalization'],
        stage_field='Status',
        stage_by_source={'quote': 'EMBASSY LEADS FROM GET QUOTE PIPELINE', 'gaql': 'EMBASSY LEADS FROM GET QUOTE PIPELINE',
                         **_same('Order Received', 'deal', 'form', 'service')},
        client=ClientFields('Client_Name', ('Email',), 'Phone', 'Address', 'Client_Comment'),
        contact_lookup='Client_Contact', constants={'File_Uploaded': False}, typed=_embassy_typed,
        tid_service='embassy_legalization', intake=True, blocking=_blocking('Status'),
        copy_label='Embassy Legalization Services',
        typed_fields={'Country_of_Legalization': TEXT, 'Document_Type': TEXT, 'File_Uploaded': BOOLEAN,
                      **TRANSLATION_ADDON_FIELDS},
        typed_values={'Translation_Required': (TRANSLATION_REQUIRED_YES,)},
    ),
    ServiceSpec(
        key='translation', label='Translation', module='Translation_Services', tab=TABS['Translation_Services'],
        stage_field='Translation_Status',
        stage_by_source={'quote': 'TRANSLATION LEADS FROM GET QUOTE PIPELINE',
                         'gaql': 'TRANSLATION LEADS FROM GET QUOTE PIPELINE',
                         'deal': 'FROM FBI TRANSLATION REQUETS',   # sic, the live display value
                         **_same('Client Placed Request', 'form', 'service')},
        client=ClientFields('Client_Name1', ('Email',), 'Phone', 'Client_Address', 'Client_Comments'),
        contact_lookup='Client_Name', constants={'Price_Per_Page': 0, 'Deadline_for_translation': _today_plus_3},
        typed=_translation_typed, tid_service='translation', intake=True, blocking=_blocking('Translation_Status'),
        copy_label='Translation Service',
        typed_fields={'Languages': TEXT, 'Translation_Type': PICKLIST, 'Pages': INTEGER, 'Price_Per_Page': CURRENCY,
                      'Deadline_for_translation': DATE},
        typed_values={'Translation_Type': tuple(v for v in TRANSLATION_TYPES.values() if v)},
        notes=_translation_notes,
    ),
    ServiceSpec(
        key='fbi', label='FBI Background Check', module='FINGERPRINT_SERVICES', tab=TABS['FINGERPRINT_SERVICES'],
        stage_field='Fingerprint_Stage', stage_by_source=FINGERPRINT_STAGE, client=FINGERPRINT_CLIENT,
        contact_lookup='Client_Name',
        constants={'Fingerprint_Service_Type': 'FBI Live Scan', 'Fingerprint_Purpose': 'Background Check'},
        typed=_fbi_typed, tid_service=None, intake=True, blocking=_blocking('Fingerprint_Stage'),
        copy_label='Fingerprinting Services', attribution=False,
        typed_fields={'Apostille_Needed': PICKLIST, 'Appointment_Type': PICKLIST, 'Number_of_Cards': INTEGER,
                      'Fingerprint_Service_Type': PICKLIST, 'Fingerprint_Purpose': PICKLIST},
        typed_values={'Apostille_Needed': ('YES', 'NO'), 'Appointment_Type': ('Office', 'Mobile')},
        notes=_fbi_notes,
    ),
    ServiceSpec(
        key='fingerprinting', label='Fingerprinting', module='FINGERPRINT_SERVICES', tab=TABS['FINGERPRINT_SERVICES'],
        stage_field='Fingerprint_Stage', stage_by_source=FINGERPRINT_STAGE, client=FINGERPRINT_CLIENT,
        contact_lookup='Client_Name', constants={}, typed=_fingerprinting_typed, tid_service=None, intake=True,
        blocking=_blocking('Fingerprint_Stage'), copy_label='Fingerprinting Services', attribution=False,
        typed_fields={'Fingerprint_Service_Type': PICKLIST, 'Appointment_Type': PICKLIST, 'Number_of_Cards': INTEGER},
        typed_values={'Fingerprint_Service_Type': tuple(v for v in FINGERPRINT_TYPES.values() if v),
                      'Appointment_Type': ('Office', 'Mobile')},
    ),
    _notary('mobile-notary', 'Mobile Notary', 'Mobile notary', address=True, typed=_mobile_notary_typed,
            notes=_mobile_notary_notes),
    _notary('office-notary', 'Office Notary', 'Office notary', notes=_office_notary_notes),
    _notary('ron', 'Online Notary (RON)', 'Online notary (RON)', notes=_ron_notes),
    _notary('notary', 'Notary', None, notes=lambda facts: ['Notary type not given'], intake=False),
    ServiceSpec(
        key='i9', label='I-9 Verification', module='I_9_Verification', tab=TABS['I_9_Verification'], stage_field='Stage',
        stage_by_source=_same('I-9 LEADS FROM GET QUOTE PIPELINE'),
        client=ClientFields('Client_Name', ('Client_Email',), 'Client_Phone', 'Address', 'Client_Comments'),
        contact_lookup='Client_Contact', constants={'Submission_Date': _today_plus_3}, typed=_none, tid_service=None,
        intake=True, blocking=_blocking('Stage'), copy_label='I-9 Employment Verification Services',
        typed_fields={'Submission_Date': DATE}, search_by='phone',
    ),
    ServiceSpec(
        key='triple-seal', label='Triple Seal', module='Triple_Seal_Apostilles', tab=TABS['Triple_Seal_Apostilles'],
        stage_field='Stage', stage_by_source={**_same(TRIPLE_SEAL_STAGE), 'form:marriage': 'Order Received'},
        client=ClientFields('Client_Name', ('Client_Email',), 'Client_Phone', 'Client_Address', 'Client_Notes_Comments'),
        contact_lookup='Client_Contact', constants={'Type_of_Legalization': 'Triple Seal'}, typed=_triple_seal_typed,
        tid_service=None, intake=False, blocking=_blocking('Stage'), copy_label=None, quote_request=False,
        typed_fields={'Type_of_Legalization': PICKLIST, 'Marriage_Info': TEXTAREA},
    ),
    ServiceSpec(
        key='fbi-apostille-deal', label='FBI Apostille', module='Deals', tab=TABS['Deals'], stage_field='Stage',
        stage_by_source=_same('FROM APOSTILLE REQUEST'),
        client=ClientFields('Name1', ('Email_1',), 'Phone', None, ''),
        contact_lookup='Client_Contact', constants={'Pipeline': 'Standard (Standard)'}, typed=_deal_typed,
        tid_service='fbi_apostille', intake=False, blocking=_blocking('Stage', 'Deal_Name'), copy_label=None,
        name_field='Deal_Name',
        typed_fields={'Pipeline': PICKLIST, 'Translated_Language': PICKLIST, **TRANSLATION_ADDON_FIELDS},
        typed_values={'Translation_Required': (TRANSLATION_REQUIRED_YES,), 'Translated_Language': DEAL_LANGUAGES},
    ),
)}

# Workflow-copy bare labels (adoption step 4) and the modules they land in.
COPY_LABELS = frozenset(spec.copy_label for spec in REGISTRY.values() if spec.copy_label)


def get(key: str) -> ServiceSpec:
    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownService(key) from None


def is_known(key: str) -> bool:
    return key in REGISTRY or key in NO_TARGET


def expand(keys: list[str], requested: list[str]) -> list[str]:
    """Service keys to act on, in order and without repeats.

    `*` = every requested line with a target (the engine then skips done, dismissed and suggested links);
    `notary` = the requested notary lines, else the `notary` alias itself. Unknown keys raise UnknownService.
    """
    out: list[str] = []
    for key in keys:
        if key == '*':
            out += [k for k in requested if k not in NO_TARGET]
        elif key in ALIASES:
            out += [k for k in requested if k in ALIASES[key]] or [key]
        elif is_known(key):
            out.append(key)
        else:
            raise UnknownService(key)
    return list(dict.fromkeys(out))


# --- Payload ---------------------------------------------------------------------------------------

def stage_kind(source_kind: str, source_key: str) -> str:
    """The stage column of §7.4 for a source: quote | gaql | deal | form[:<order_type>] | service."""
    if source_kind == 'quote':
        return 'quote'
    if source_kind == 'form':
        parts = source_key.split(':')
        return f'form:{parts[1]}' if len(parts) > 2 else 'form'
    code = source_key.split(':')[1] if source_key.count(':') >= 2 else ''
    return {'gaql': 'gaql', 'lead': 'gaql', 'deal': 'deal'}.get(code, 'service')


def stage_for(spec: ServiceSpec, facts: 'Facts', source_kind: str) -> str:
    kind = stage_kind(source_kind, facts.source.get('key', ''))
    if spec.key == 'apostille' and kind == 'quote' and facts.typed.get('doc_kind') == 'fbi_report' \
            and flags.apostille_fbi_stage():
        return flags.apostille_fbi_stage()   # QUESTIONS A6; empty by default → the generic intake stage
    return spec.stage_by_source.get(kind) or spec.stage_by_source.get(kind.split(':')[0]) \
        or spec.stage_by_source['quote']


def record_name(spec: ServiceSpec, facts: 'Facts') -> str:
    """`Apostille · Maria Fernández · Q-482913` (≤120; the client name is shortened, never the reference)."""
    prefix = TEST_PREFIX if facts.source.get('is_test') else ''
    label, ref = f'{prefix}{spec.label}', facts.source.get('ref_label') or ''
    name = ' '.join(str(facts.client.get('name') or '').split())
    parts = [label, name, ref] if name else [label, ref]
    text = ' · '.join(part for part in parts if part)
    if len(text) <= NAME_MAX or not name:
        return text[:NAME_MAX]
    room = NAME_MAX - len(f'{label} ·  · {ref}') - len(ELLIPSIS)
    return f'{label} · {name[:max(room, 1)]}{ELLIPSIS} · {ref}'[:NAME_MAX]


def comments_block(spec: ServiceSpec, facts: 'Facts', *, extra: list[str] | None = None,
                   limit: int = COMMENTS_DEFAULT_MAX) -> str:
    """The comments block of §7.4; the client message is cut first when the block is too long."""
    quote, line, typed = facts.quote or {}, facts.line or {}, facts.typed
    ref = facts.source.get('ref_label') or ''
    kind = facts.source.get('kind')
    origin = f'from {SOURCE_ORIGINS.get(kind, "Zoho record")}'
    if kind == 'quote' and (quote.get('n_services') or 1) > 1:
        origin += f' ({quote["n_services"]} services: {quote.get("services_label")})'
    head = [f'[{ref}] {spec.label} · {origin}']
    summary = line.get('summary_server') or ''
    if ' — ' in summary:
        summary = summary.split(' — ', 1)[1]
    if summary and summary != spec.label:
        head.append(summary)
    if line.get('price_text'):
        head.append(f'Price shown: {line["price_text"]}')
    if typed.get('channel_label') or typed.get('when_label'):
        contact = f'Contact: {typed.get("channel_label") or "no preference"}'
        head.append(f'{contact} · When: {typed["when_label"]}' if typed.get('when_label') else contact)
    if not (facts.client.get('email') or facts.client.get('phone_e164') or facts.client.get('phone_raw')):
        head.append(NO_CONTACT_DETAILS)
    head += spec.notes(facts) + list(extra or [])
    url = facts.source.get('url')
    tail = [f'{"Quote" if facts.source.get("module") == "Get_A_Quote_Leads" else "Source"}: {url}'] if url else []

    message = ' '.join(str(typed.get('message') or '').split())
    full = '\n'.join(head + ([f'Message: {message}'] if message else []) + tail)
    if len(full) <= limit:
        return full
    fixed = '\n'.join(head + tail)
    room = limit - len(fixed) - len('\nMessage: ' + ELLIPSIS)
    if message and room > 0:
        return '\n'.join(head + [f'Message: {message[:room]}{ELLIPSIS}'] + tail)
    return fixed if len(fixed) <= limit else fixed[:limit - 1] + ELLIPSIS


def field_length(module: str, api_name: str, default: int = COMMENTS_DEFAULT_MAX) -> int:
    meta = (schema.get_fields(module) or {}).get(api_name) or {}
    return int(meta.get('length') or default)


def build(spec: ServiceSpec, facts: 'Facts', *, source_kind: str, key: str, extra_comments: list[str] | None = None,
          stage: str | None = None) -> dict:
    """The create payload of one service (before the guard): common fields, stage, client, constants, typed."""
    client = facts.client
    payload: dict = {spec.name_field: record_name(spec, facts), KEY_FIELD: key,
                     spec.stage_field: stage or stage_for(spec, facts, source_kind)}
    gaql_id = facts.source.get('id') if facts.source.get('module') == 'Get_A_Quote_Leads' else None
    if spec.quote_request and gaql_id:
        payload['Quote_Request'] = {'id': str(gaql_id)}
    if facts.quote and facts.quote.get('reference'):
        payload['Quote_Reference'] = facts.quote['reference']
    if facts.source.get('url'):
        payload['Created_From'] = facts.source['url']
    if client.get('contact_id'):
        payload[spec.contact_lookup] = {'id': str(client['contact_id'])}
    if spec.attribution and facts.attribution_id:
        payload['Attribution_Record'] = {'id': str(facts.attribution_id)}
    owner = flags.owner_for(spec.key)
    if owner:
        payload['Owner'] = {'id': owner}

    if spec.client.name and client.get('name'):
        payload[spec.client.name] = client['name']
    if client.get('email'):
        payload.update({name: client['email'] for name in spec.client.emails})
    phone = client.get('phone_e164') or client.get('phone_raw')
    if spec.client.phone and phone:
        payload[spec.client.phone] = phone
    if spec.client.address and client.get('address'):
        payload[spec.client.address] = client['address']

    if flags.parity_constants():
        payload.update({name: value() if callable(value) else value for name, value in spec.constants.items()})
    payload.update(spec.typed(facts))
    if spec.client.comments:
        payload[spec.client.comments] = comments_block(
            spec, facts, extra=extra_comments, limit=field_length(spec.module, spec.client.comments))
    return payload


def enrich_fields(spec: ServiceSpec) -> tuple[str, ...]:
    """Client and typed fields that adoption fills when the copy left them blank."""
    client = spec.client
    names = [client.name, *client.emails, client.phone, client.address, *spec.typed_fields]
    return tuple(name for name in dict.fromkeys(names) if name and name != spec.client.comments)


# --- Validation ------------------------------------------------------------------------------------

def _field_types(spec: ServiceSpec) -> dict[str, tuple[str, ...]]:
    types = {spec.name_field: TEXT, KEY_FIELD: TEXT, spec.stage_field: PICKLIST, spec.contact_lookup: LOOKUP,
             'Quote_Reference': TEXT, 'Created_From': WEBSITE}
    if spec.quote_request:
        types['Quote_Request'] = LOOKUP
    if spec.attribution:
        types['Attribution_Record'] = LOOKUP
    if spec.tid_service:
        types['Tracking_ID'] = TEXT
    client = spec.client
    if client.name:
        types[client.name] = TEXT
    types.update({name: EMAIL for name in client.emails})
    if client.phone:
        types[client.phone] = PHONE
    if client.address:
        types[client.address] = TEXT
    if client.comments:
        types[client.comments] = TEXTAREA
    types.update(spec.typed_fields)
    return types


def _picklist_values(spec: ServiceSpec) -> dict[str, tuple[str, ...]]:
    values = {spec.stage_field: tuple(spec.stages())}
    for name, value in spec.constants.items():
        if spec.typed_fields.get(name) == PICKLIST and isinstance(value, str):
            values[name] = (*values.get(name, ()), value)
    for name, allowed in spec.typed_values.items():
        values[name] = (*values.get(name, ()), *allowed)
    return values


def service_problems(spec: ServiceSpec, fields: Mapping[str, dict]) -> list[str]:
    """Problems of one service against its module's metadata (by api_name, normalised as crm.schema stores it)."""
    problems = []
    for name, types in _field_types(spec).items():
        meta = fields.get(name)
        if meta is None:
            problems.append(f'{spec.module}.{name}: field missing ({spec.key})')
        elif meta.get('data_type') not in types:
            problems.append(f'{spec.module}.{name}: type {meta.get("data_type")}, expected {"/".join(types)} '
                            f'({spec.key})')
    for name, values in _picklist_values(spec).items():
        active = (fields.get(name) or {}).get('pick_list_values')
        if active is None:
            continue   # reported above as missing
        for value in dict.fromkeys(values):
            if value not in active:
                problems.append(f"{spec.module}.{name}: value '{value}' not active ({spec.key})")
    for value in spec.stages():
        if AUTOMATED_MARK in value:
            problems.append(f"{spec.module}.{spec.stage_field}: intake value '{value}' is an automated stage")
        if CLOSED_STAGE_PATTERN.search(value):
            problems.append(f"{spec.module}.{spec.stage_field}: intake value '{value}' looks closed")
    return problems


def validate(metadata: Mapping[str, dict]) -> list[str]:
    """Problems of every service whose module is in `metadata` (module → fields by api_name); [] = OK."""
    problems: list[str] = []
    for spec in REGISTRY.values():
        if spec.module in metadata:
            problems += service_problems(spec, metadata[spec.module])
    return list(dict.fromkeys(problems))
