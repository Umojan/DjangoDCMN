"""The desired Zoho state (DESIGN §6.1–6.3), read by the `zoho_schema` command and by WP7.

Additions only (invariant 3): nothing here renames, removes or re-types an existing field or value.
Each label is the api_name with spaces (ASSUMPTION A1): the field-create API derives the api_name from the
label, and the command aborts when Zoho derives another one. Friendlier labels are a later UI rename
(ZOHO-SETUP Z4.3), which keeps the api_name. The list order of each module is the apply order (Z3.2).
"""

from dataclasses import dataclass, replace

GAQL = 'Get_A_Quote_Leads'
GAQL_LAYOUT_ID = '6686316000002178172'   # prod GAQL "Standard" layout: the org fingerprint of --apply (§6.6)
MAX_CUSTOM_LOOKUPS = 3                    # proven by Triple Seal [F3]; a 4th custom lookup is BLOCKED
RELATED_LIST_STEP = 'Z4.2'                # related-list titles are renamed by hand (A4)
DCMN_TOOLTIP = 'Filled by DCMN — manual edits are overwritten'

# Zoho display value per quote service key (DESIGN §5.3, QUESTIONS A4): Primary_Service, Services_Requested,
# Services_Created and the subform Service column.
SERVICE_VALUES: dict[str, str] = {
    'apostille': 'Apostille',
    'embassy': 'Embassy Legalization',
    'translation': 'Translation',
    'fbi': 'FBI Background Check',
    'retrieval': 'Document Retrieval',
    'fingerprinting': 'Fingerprinting',
    'mobile-notary': 'Mobile Notary',
    'ron': 'Online Notary (RON)',
    'office-notary': 'Office Notary',
    'i9': 'I-9 Verification',
    'other': 'Other / Not Sure',
}
SERVICES = tuple(SERVICE_VALUES.values())


@dataclass(frozen=True)
class FieldSpec:
    api_name: str
    label: str
    data_type: str        # text|textarea|picklist|multiselectpicklist|integer|boolean|website|lookup|subform
    length: int | None = None
    values: tuple[str, ...] = ()              # picklist display values, in order
    lookup_module: str | None = None
    related_list_title: str | None = None     # title on the quote; renamed by hand (RELATED_LIST_STEP)
    manual: str | None = None                 # ZOHO-SETUP step id: made in the UI, never by the API
    fallback: 'FieldSpec | None' = None       # swapped in by PR if Zoho refuses this type (A2)
    actual_values: tuple[str, ...] = ()       # picklist actual values, when they differ from `values`
    tooltip: str | None = None
    existing: bool = False                    # already in Zoho: checked, never created
    columns: tuple['FieldSpec', ...] = ()     # subform columns, checked after the manual creation

    def __post_init__(self):
        if self.actual_values and len(self.actual_values) != len(self.values):
            raise ValueError(f'{self.api_name}: actual_values must match values')

    def create_payload(self) -> dict:
        """One item of `POST /crm/v6/settings/fields` (ZOHO-SETUP Z3.1)."""
        payload: dict = {'field_label': self.label, 'data_type': self.data_type}
        if self.data_type in ('text', 'integer') and self.length:
            payload['length'] = self.length
        if self.data_type == 'textarea':
            payload['textarea'] = {'type': 'small'}
        if self.values:
            actual = self.actual_values or self.values
            payload['pick_list_values'] = [
                {'display_value': display, 'actual_value': value} for display, value in zip(self.values, actual)
            ]
        if self.lookup_module:
            payload['lookup'] = {'module': {'api_name': self.lookup_module}}
        if self.tooltip:
            payload['tooltip'] = {'name': 'static_text', 'value': self.tooltip}
        return payload


def _text(api_name: str, length: int, **extra) -> FieldSpec:
    return FieldSpec(api_name, api_name.replace('_', ' '), 'text', length=length, **extra)


def _picklist(api_name: str, values: tuple[str, ...], **extra) -> FieldSpec:
    return FieldSpec(api_name, api_name.replace('_', ' '), 'picklist', values=values, **extra)


def _quote_request(title: str) -> FieldSpec:
    return FieldSpec('Quote_Request', 'Quote Request', 'lookup', lookup_module=GAQL, related_list_title=title)


# --- Get_A_Quote_Leads (§6.1) -------------------------------------------------------------------

# Not unique (C13): a re-sent quote may share it.
QUOTE_REFERENCE = _text('Quote_Reference', 20, tooltip='Filled by DCMN — Q-number of the web quote')
CREATED_FROM = FieldSpec('Created_From', 'Created From', 'website')

GET_A_QUOTE_LEADS = FieldSpec('GET_A_QUOTE_LEADS', 'GET A QUOTE LEADS', 'picklist', manual='Z4.1', existing=True)

QUOTE_LINES = FieldSpec(
    'Quote_Lines', 'Quote Lines', 'subform', manual='Z11',
    columns=(
        _picklist('Service', SERVICES),
        _text('Service_Key', 24),
        _text('Line_Ref', 20),
        FieldSpec('Details', 'Details', 'textarea', length=2000),
        _text('Price_Shown', 255),
        _picklist('Line_Status', ('Requested', 'Suggested', 'Creating', 'Created', 'Linked', 'Needs decision',
                                  'Failed', 'No pipeline', 'Dismissed')),
        FieldSpec('Service_Record', 'Service Record', 'website'),
        _text('Done_By', 120),
    ),
)

GAQL_FIELDS = [
    _picklist('Lead_Language', ('English', 'Spanish')),            # the Z2 canary
    QUOTE_REFERENCE,
    _picklist('Quote_Status', ('New', 'Contacted', 'Quoted', 'Accepted', 'Lost', 'Spam')),   # QUESTIONS A3
    _picklist('Service_Progress', ('Not started', 'Partially created', 'All created', 'Nothing to create'),
              tooltip=DCMN_TOOLTIP),
    FieldSpec('Lines_Pending', 'Lines Pending', 'integer', length=9),
    _picklist('Primary_Service', SERVICES),
    FieldSpec('Services_Requested', 'Services Requested', 'multiselectpicklist', values=SERVICES,
              fallback=_text('Services_Requested_Text', 255)),
    FieldSpec('Services_Created', 'Services Created', 'multiselectpicklist', values=SERVICES, tooltip=DCMN_TOOLTIP),
    FieldSpec('Quote_Lines_Text', 'Quote Lines Text', 'textarea', length=2000, tooltip=DCMN_TOOLTIP),
    _picklist('Urgency', ('No rush', 'As soon as possible', 'By a specific date', 'Not sure')),
    _picklist('Contact_Preference', ('Call', 'Text', 'WhatsApp', 'Email')),
    FieldSpec('WhatsApp_Opt_In', 'WhatsApp Opt In', 'boolean'),
    FieldSpec('SMS_Opt_In', 'SMS Opt In', 'boolean'),
    _text('Consent_Version', 60),
    _text('Quote_Source_Page', 255),
    GET_A_QUOTE_LEADS,
    QUOTE_LINES,
]

# --- Translation fields (§6.3) ------------------------------------------------------------------

# The Deals display values verbatim, with the Deals actual values `Yes` / `No` [F5].
TRANSLATION_REQUIRED = _picklist('Translation_Required', ('Yes -Translate', 'No - Translation'),
                                 actual_values=('Yes', 'No'))
TRANSLATION_LANGUAGES = [_text('Translation_Source_Language', 60), _text('Translation_Target_Language', 60)]

# --- Target modules (§6.2), GAQL first: the apply order of ZOHO-SETUP Z3.2 ------------------------

SPEC: dict[str, list[FieldSpec]] = {
    GAQL: GAQL_FIELDS,
    'Apostille_Services': [
        _quote_request('Apostille from this quote'), QUOTE_REFERENCE, CREATED_FROM,
        TRANSLATION_REQUIRED, *TRANSLATION_LANGUAGES,
    ],
    'Embassy_Legalization': [
        _quote_request('Embassy from this quote'), QUOTE_REFERENCE, CREATED_FROM,
        TRANSLATION_REQUIRED, *TRANSLATION_LANGUAGES,
    ],
    'Translation_Services': [
        _quote_request('Translation from this quote'), QUOTE_REFERENCE, CREATED_FROM,
    ],
    'FINGERPRINT_SERVICES': [
        _quote_request('Fingerprinting / FBI from this quote'), QUOTE_REFERENCE, CREATED_FROM,
        _picklist('Translation_Needed', ('YES', 'NO'), existing=True),
        *TRANSLATION_LANGUAGES,
    ],
    'Notary_Services': [
        _quote_request('Notary from this quote'), QUOTE_REFERENCE, CREATED_FROM,
    ],
    'I_9_Verification': [
        _quote_request('I-9 from this quote'), QUOTE_REFERENCE, CREATED_FROM,
    ],
    'Deals': [
        _quote_request('FBI apostille from this quote'), QUOTE_REFERENCE, CREATED_FROM,
        replace(TRANSLATION_REQUIRED, existing=True),
        _picklist('Translated_Language', ('Portuguese', 'Spanish', 'Arabic', 'Chinese', 'French'), existing=True),
        *TRANSLATION_LANGUAGES,
    ],
    # No Quote_Request: it would be the 4th custom lookup, which is unproven [F3].
    'Triple_Seal_Apostilles': [
        QUOTE_REFERENCE, CREATED_FROM,
        FieldSpec('Translation_Required', 'Translation Required', 'boolean', existing=True),
        *TRANSLATION_LANGUAGES,
    ],
}

# Options added by hand to an existing picklist (C11: Django never edits an existing picklist).
MANUAL_OPTIONS: dict[tuple[str, str], tuple[str, ...]] = {(GAQL, 'GET_A_QUOTE_LEADS'): ('Web Quote Request',)}


def find(module: str, api_name: str) -> FieldSpec | None:
    return next((spec for spec in SPEC.get(module, ()) if spec.api_name == api_name), None)
