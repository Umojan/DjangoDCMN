"""The `quote.v3` contract in Python (DESIGN §3, §5.3, §5.4): service keys, labels, answer enums, prices.

The JSON Schema in `quotes/contract/quote.v3.schema.json` (DESIGN §3.9) is normative for the client; this module
holds the same enums for the server, plus what the schema cannot carry: v5 labels, the Zoho display values and
the approved price table. The fixtures in `quotes/contract/fixtures/` are the executable contract shared with the
hub (WP8). Any change here is a contract change: it updates the schema, the fixtures and the hub copy together.
"""

import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 'quote.v3'
CONTRACT_DIR = Path(__file__).resolve().parent / 'contract'
DATA_DIR = Path(__file__).resolve().parent / 'data'
SCHEMA_PATH = CONTRACT_DIR / 'quote.v3.schema.json'
FIXTURES_DIR = CONTRACT_DIR / 'fixtures'

FALLBACK = {'phone': '+12022470837', 'email': 'support@dcmobilenotary.com'}
MAX_BODY_BYTES = 64 * 1024
MAX_SERVICES = 11

# --- Services -----------------------------------------------------------------------------------

SERVICE_KEYS: tuple[str, ...] = (
    'apostille', 'embassy', 'translation', 'fbi', 'retrieval', 'fingerprinting',
    'mobile-notary', 'ron', 'office-notary', 'i9', 'other',
)

# v5 tile labels (EN, report 03 §2.1) and the proto ES names (not yet signed off, spec Q13).
SERVICE_LABELS: dict[str, tuple[str, str]] = {
    'apostille': ('Apostille', 'Servicios de apostilla'),
    'embassy': ('Embassy legalization', 'Legalización consular'),
    'translation': ('Translation', 'Servicios de traducción'),
    'fbi': ('FBI background check', 'Verificación de antecedentes del FBI'),
    'retrieval': ('Records retrieval', 'Obtención de documentos y actas certificadas'),
    'fingerprinting': ('Fingerprinting', 'Huellas dactilares'),
    'mobile-notary': ('Mobile notary', 'Notarización a domicilio'),
    'ron': ('Online notary (RON)', 'Notarización en línea (RON)'),
    'office-notary': ('Office notary', 'Notarización en oficina'),
    'i9': ('I-9 verification', 'Verificación I-9'),
    'other': ('Other / not sure', 'Otro / No estoy seguro'),
}

# Zoho display values of Primary_Service, Services_Requested, Services_Created (DESIGN §5.3; QUESTIONS A4).
SERVICE_DISPLAY: dict[str, str] = {
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

# Services with no target module yet (DESIGN §7.4 `NO_TARGET`): the line reads NO PIPELINE.
NO_TARGET_SERVICES = frozenset({'retrieval', 'other'})

LINE_SOURCES = ('user', 'picker', 'url', 'referrer', 'guide', 'addon', 'tip', 'route_switch')
PRIMARY_SOURCES = ('url', 'referrer', 'guide')     # DESIGN §5.4
ADDON_PARENTS = ('apostille', 'embassy')            # addon_of, switched_from, addons[] owners
ADDON_SERVICES = ('translation',)
ANSWER_SOURCES = ('user', 'url', 'referrer', 'guide', 'route_switch')
ROUTE_EXPECTED = ('apostille', 'embassy')
ROUTE_STATUSES = ('pending', 'kept')
PRICE_NOTES = ('other_states',)

# --- Answers (DESIGN §3.9 per-service schemas, report 03 §3.1) ------------------------------------

ENUM, COUNT, COUNTRY, US_STATE, LANGUAGE, ZIP, TEXT = 'enum', 'count', 'country', 'us_state', 'language', 'zip', 'text'
COUNT_MIN, COUNT_MAX = 1, 99
TEXT_MAX = 1000


@dataclass(frozen=True)
class Answer:
    kind: str
    values: tuple[str, ...] = ()


APOSTILLE_DOCS = ('fbi_report', 'naturalization', 'federal_other', 'vital', 'education', 'court',
                  'corporate', 'notarized', 'other', 'unsure')
FEDERAL_DOCS = frozenset({'fbi_report', 'naturalization', 'federal_other'})
STATE_DOCS = frozenset({'vital', 'education', 'court', 'corporate', 'notarized', 'other'})  # apostille.state shown
YES_NO_UNSURE = ('yes', 'no', 'unsure')

ANSWERS: dict[str, dict[str, Answer]] = {
    'apostille': {
        'doc': Answer(ENUM, APOSTILLE_DOCS),
        'country': Answer(COUNTRY),
        'state': Answer(US_STATE),
        'fbi_pdf': Answer(ENUM, ('yes', 'printout', 'not_yet')),
        'count': Answer(COUNT),
    },
    'embassy': {
        'kind': Answer(ENUM, ('personal', 'corporate', 'both', 'unsure')),
        'country': Answer(COUNTRY),
        'count': Answer(COUNT),
        'doc_from_apostille': Answer(ENUM, APOSTILLE_DOCS),
    },
    'translation': {
        'from': Answer(LANGUAGE),
        'to': Answer(LANGUAGE),
        'kind': Answer(ENUM, ('certified_digital', 'notarized_wet', 'traduccion_jurada', 'unsure')),
        'pages': Answer(COUNT),
    },
    'fbi': {
        'apostille': Answer(ENUM, YES_NO_UNSURE),
        'where': Answer(ENUM, ('dc', 'arlington', 'mobile', 'unsure')),
        'people': Answer(COUNT),
    },
    'retrieval': {
        'record': Answer(TEXT),
    },
    'fingerprinting': {
        'type': Answer(ENUM, ('livescan', 'fd258', 'atf', 'fdle', 'unsure')),
        'where': Answer(ENUM, ('office', 'mobile', 'unsure')),
        'people': Answer(COUNT),
    },
    'mobile-notary': {
        # `contract` is accepted for v2 clients (DESIGN §3.3)
        'doc': Answer(ENUM, ('poa', 'real_estate', 'facility', 'affidavit', 'estate', 'other', 'unsure', 'contract')),
        'zip': Answer(ZIP),
        'when': Answer(ENUM, ('today', 'tomorrow', 'this_week', 'flexible')),
    },
    'ron': {
        'doc': Answer(ENUM, ('poa', 'affidavit', 'contract', 'real_estate', 'will_trust', 'other', 'unsure')),
        'apostille': Answer(ENUM, YES_NO_UNSURE),
    },
    'office-notary': {
        'doc': Answer(ENUM, ('poa', 'affidavit', 'estate', 'contract', 'other', 'unsure')),
        'office': Answer(ENUM, ('dc', 'arlington', 'ny', 'any')),
    },
    'i9': {},
    'other': {
        'text': Answer(TEXT),
    },
}

# EN labels of the enum answers, as the v5 form shows them (report 03 §3.1); used by quotes.render and crm.
_NOTARY_DOCS = {
    'poa': 'Power of attorney', 'affidavit': 'Affidavit', 'estate': 'Will, trust or estate',
    'contract': 'Contract or agreement', 'other': 'Something else', 'unsure': 'Not sure',
}
APOSTILLE_DOC_LABELS = {
    'fbi_report': 'FBI background check', 'naturalization': 'Naturalization certificate',
    'federal_other': 'Other federal document', 'vital': 'Birth, marriage or death',
    'education': 'Diploma or transcript', 'court': 'Divorce or court order', 'corporate': 'Company documents',
    'notarized': 'Notarized document', 'other': 'Something else', 'unsure': 'Not sure',
}
ANSWER_LABELS: dict[tuple[str, str], dict[str, str]] = {
    ('apostille', 'doc'): APOSTILLE_DOC_LABELS,
    ('apostille', 'fbi_pdf'): {'yes': 'Yes', 'printout': 'Only a printout', 'not_yet': 'Not yet'},
    ('embassy', 'kind'): {'personal': 'Personal', 'corporate': 'Company', 'both': 'Both', 'unsure': 'Not sure'},
    ('embassy', 'doc_from_apostille'): APOSTILLE_DOC_LABELS,
    ('translation', 'kind'): {
        'certified_digital': 'Certified (digital)', 'notarized_wet': 'Certified with notarization (wet signature)',
        'traduccion_jurada': 'Traducción jurada', 'unsure': 'Not sure',
    },
    ('fbi', 'apostille'): {'yes': 'Yes', 'no': 'No', 'unsure': 'Not sure'},
    ('fbi', 'where'): {'dc': 'DC office', 'arlington': 'Arlington office', 'mobile': 'At my location',
                       'unsure': 'Not sure'},
    ('fingerprinting', 'type'): {'livescan': 'Live Scan', 'fd258': 'FD-258 ink cards', 'atf': 'ATF', 'fdle': 'FDLE',
                                 'unsure': 'Not sure'},
    ('fingerprinting', 'where'): {'office': 'At our office', 'mobile': 'At my location', 'unsure': 'Not sure'},
    ('mobile-notary', 'doc'): {**_NOTARY_DOCS, 'real_estate': 'Real estate closing',
                               'facility': 'Hospital or senior living'},
    ('mobile-notary', 'when'): {'today': 'Today', 'tomorrow': 'Tomorrow', 'this_week': 'This week',
                                'flexible': 'Flexible'},
    ('ron', 'doc'): {**_NOTARY_DOCS, 'real_estate': 'Real estate document', 'will_trust': 'Will or trust'},
    ('ron', 'apostille'): {'yes': 'Yes', 'no': 'No', 'unsure': 'Not sure'},
    ('office-notary', 'doc'): _NOTARY_DOCS,
    ('office-notary', 'office'): {'dc': 'Washington, DC', 'arlington': 'Arlington, VA', 'ny': 'New York, NY',
                                  'any': 'Any office'},
}

# `fbi.apostille=yes` / `ron.apostille=yes` without an apostille line imply one (report 03 §6.10).
IMPLIED_BY: dict[tuple[str, str], tuple[str, str]] = {
    ('fbi', 'apostille'): ('apostille', 'client said the FBI report needs an apostille'),
    ('ron', 'apostille'): ('apostille', 'client said the notarized document needs an apostille'),
}

US_STATES: dict[str, str] = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas', 'CA': 'California', 'CO': 'Colorado',
    'CT': 'Connecticut', 'DE': 'Delaware', 'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia',
    'HI': 'Hawaii', 'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan',
    'MN': 'Minnesota', 'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York', 'NC': 'North Carolina',
    'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania',
    'RI': 'Rhode Island', 'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
    'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming',
}
STATE_UNSURE = 'unsure'

# --- Contact (DESIGN §3.4) ------------------------------------------------------------------------

CHANNELS = ('call', 'text', 'whatsapp', 'email')
CHANNEL_DISPLAY = {'call': 'Call', 'text': 'Text', 'whatsapp': 'WhatsApp', 'email': 'Email'}
WHEN_VALUES = ('standard', 'asap', 'by_date', 'unsure')
WHEN_LABELS = {'standard': 'No rush', 'asap': 'As soon as possible', 'by_date': 'By a specific date',
               'unsure': 'Not sure'}
# v5 sends the radio labels; "By a date" is the short label on narrow screens.
WHEN_ALIASES = {**{label.lower(): key for key, label in WHEN_LABELS.items()}, 'by a date': 'by_date'}
TERMS_METHOD = 'statement'
LOCALES = ('en', 'es')
TEST_NAME_PREFIX = 'zz test'

# --- Prices (DESIGN §5.4; QUESTIONS A10) ---------------------------------------------------------

PRICE_TABLE_VERSION = 'aziz-pending-2026-11-26'
PRICE_KINDS = ('from', 'review', 'route_pending')
PRICE_TIERS = ('federal', 'dc')


@dataclass(frozen=True)
class ApprovedPrice:
    amount_usd: int
    per_document_usd: int
    plus_embassy_fee: bool


APPROVED_PRICES: dict[tuple[str, str | None], ApprovedPrice] = {
    ('apostille', 'federal'): ApprovedPrice(125, 25, False),
    ('apostille', None): ApprovedPrice(125, 25, False),
    ('apostille', 'dc'): ApprovedPrice(195, 25, False),
    ('embassy', None): ApprovedPrice(495, 50, True),
}
PRICE_TIER_LABELS = {'federal': 'Federal', 'dc': 'DC'}
REVIEW_PRICE_TEXT = 'Price after free review'

# --- Uploads (config endpoint; WP9) -------------------------------------------------------------

UPLOAD_LIMITS = {'max_files': 5, 'max_mb': 10,
                 'types': ['application/pdf', 'image/jpeg', 'image/png', 'image/heic']}


# --- Reference data (copies of the v5 source of truth, hub proto/src/*.json) --------------------

def fold(text: str) -> str:
    """Case- and accent-insensitive key for matching names ('Inglés' == 'ingles')."""
    decomposed = unicodedata.normalize('NFKD', str(text).strip().casefold())
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch))


@lru_cache(maxsize=1)
def languages() -> dict[str, tuple[str, str]]:
    """slug → (English name, Spanish name), the fixed `english` and `other` included."""
    data = json.loads((DATA_DIR / 'languages.json').read_text(encoding='utf-8'))
    entries = {**data['fixed'], **data['languages']}
    return {slug: (names[0], names[1]) for slug, names in entries.items()}


@lru_cache(maxsize=1)
def language_lookup() -> dict[str, str]:
    """Folded slug, English or Spanish name → slug."""
    lookup: dict[str, str] = {}
    for slug, (en, es) in languages().items():
        for name in (slug, en, es):
            lookup.setdefault(fold(name), slug)
    return lookup


@lru_cache(maxsize=1)
def countries() -> dict[str, str]:
    """ISO-2 → English name, for the 237 countries the form offers (US and its territories excluded)."""
    data = json.loads((DATA_DIR / 'countries.json').read_text(encoding='utf-8'))
    return {code: entry[0] for code, entry in data['countries'].items()}


@lru_cache(maxsize=1)
def country_lookup() -> dict[str, str]:
    """Folded ISO-2 code, English name or alias → ISO-2."""
    data = json.loads((DATA_DIR / 'countries.json').read_text(encoding='utf-8'))
    lookup = {fold(code): code for code in data['countries']}
    for code, entry in data['countries'].items():
        lookup.setdefault(fold(entry[0]), code)
    for alias, code in data.get('aliases', {}).items():
        if code in data['countries']:
            lookup.setdefault(fold(alias), code)
    return lookup


def language_name(slug: str) -> str:
    return languages().get(slug, (slug, slug))[0]


def country_name(code: str) -> str:
    return countries().get(code, code)
