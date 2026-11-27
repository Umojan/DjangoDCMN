"""Validation and normalisation of a `quote.v3` body (DESIGN §3.2–3.4, §3.6, §5.4).

A hard `400` (`ContractError`) only for what the client can fix: the contact fields, empty or unknown services,
an unsupported schema, a malformed `submission_id`. Everything else is lenient: a bad answer is dropped into
`QuoteLine.dropped_answers`, a bad optional value falls back to its default, and both leave a warning.
"""

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable
from urllib.parse import urlsplit

import phonenumbers
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator

from . import contract as c

NAME_MIN, NAME_MAX = 2, 100
EMAIL_MAX, EMAIL_ZOHO_MAX = 254, 100
PHONE_RAW_MAX, PHONE_MIN_DIGITS = 40, 7
MESSAGE_MAX = 2000

TOP_LEVEL_KEYS = frozenset({
    'schema_version', 'form_version', 'submission_id', 'locale', 'page', 'entry', 'timing', 'services',
    'price_notes', 'contact', 'consent', 'files', 'attribution', 'turnstile_token', 'website_confirm', 'fax',
    'client',
})
SECRET_KEYS = ('turnstile_token', 'website_confirm', 'fax')   # never stored in raw_payload
ENTRY_TYPES = ('direct', 'url_param', 'group', 'referrer', 'guide')
ENTRY_PARAMS = ('service', 'doc', 'group', 'country', 'state', 'speed')

# `detail` copy for the first error (the v5 field messages, report 03 §4).
DETAIL_COPY = {
    ('contact.full_name', 'required'): 'Enter your full name.',
    ('contact.full_name', 'too_long'): 'Full name must be 100 characters or fewer.',
    ('contact.email', 'required'): 'Enter your email address.',
    ('contact.email', 'invalid'): 'Enter a valid email, like name@example.com.',
    ('contact.phone', 'required'): 'Enter your phone number.',
    ('contact.phone', 'invalid'): 'Enter a phone number, like (202) 555-0100.',
    ('contact.deadline', 'invalid'): 'Enter a valid date.',
    ('contact.deadline', 'past_date'): 'Choose a date that is today or later.',
    ('contact.message', 'too_long'): 'Your note must be 2,000 characters or fewer.',
    ('services', 'required'): 'Choose at least one service, or choose Other / not sure.',
}
RELOAD_DETAIL = 'Something went wrong with this page. Please reload it and try again.'

_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')
_CONTROL_SINGLE_LINE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_ZIP = re.compile(r'^\d{5}$')
_validate_email = EmailValidator()


class ContractError(Exception):
    """A request the client must fix: `errors` maps a field path to codes; `detail` is the first error's copy."""

    status = 400

    def __init__(self, errors: dict[str, list[str]]):
        self.errors = errors
        path, codes = next(iter(errors.items()))
        self.detail = DETAIL_COPY.get((path, codes[0]), RELOAD_DETAIL)
        super().__init__(self.detail)

    def response_body(self) -> dict:
        return {'ok': False, 'detail': self.detail, 'errors': self.errors}


@dataclass
class NormalisedQuote:
    """`core`: QuoteSubmission column → value; `lines`: QuoteLine column → value, in position order."""

    core: dict
    lines: list[dict]
    warnings: list[str]
    needs_review: bool
    core_hash: str


def normalise(body: dict, *, today: date) -> NormalisedQuote:
    """Validate and normalise a request body; raises ContractError. `today` is the America/New_York date."""
    if not isinstance(body, dict):
        raise ContractError({'body': ['invalid']})
    if body.get('schema_version') != c.SCHEMA_VERSION:
        raise ContractError({'schema_version': ['unsupported']})

    errors: dict[str, list[str]] = {}
    warnings = [f'{key}:ignored' for key in body if key not in TOP_LEVEL_KEYS]
    contact = _contact(body.get('contact'), today, errors, warnings)
    lines = _lines(body.get('services'), errors, warnings)
    submission_id = _submission_id(body.get('submission_id'), errors)
    if errors:
        raise ContractError(errors)

    core = {
        'submission_id': submission_id,
        'schema_version': c.SCHEMA_VERSION,
        'form_version': _form_version(body.get('form_version'), warnings),
        'locale': _locale(body.get('locale'), warnings),
        'is_test': contact['full_name'].casefold().startswith(c.TEST_NAME_PREFIX),
        **contact,
        **_consent(body.get('consent'), contact['preferred_channel'], warnings),
        'primary_service': next(line['service_key'] for line in lines if line['is_primary']),
        'service_keys': [line['service_key'] for line in lines],
        'price_notes': _price_notes(body.get('price_notes')),
        **_page(body.get('page'), warnings),
        'entry': _entry(body.get('entry')),
        'timing': _timing(body.get('timing'), warnings),
        'client': _client(body.get('client')),
        'files_dropped': _files_dropped(body.get('files'), warnings),
        'raw_payload': {key: value for key, value in body.items() if key not in SECRET_KEYS},
    }
    core['effective_channel'] = effective_channel(
        core['preferred_channel'], core['whatsapp_opt_in'], core['sms_opt_in'],
    )
    needs_review = any(line['needs_review'] for line in lines)
    if len(core['email']) > EMAIL_ZOHO_MAX:
        warnings.append('contact.email:over_100')
        needs_review = True
    material = hash_material(core, [(line['service_key'], line['answers']) for line in lines])
    return NormalisedQuote(core=core, lines=lines, warnings=warnings, needs_review=needs_review,
                           core_hash=hash_of(material))


def effective_channel(preferred: str, whatsapp_opt_in: bool, sms_opt_in: bool) -> str:
    """`prefOn()` semantics (report 03 §4): WhatsApp or text without its opt-in means call or email ('')."""
    if preferred == 'whatsapp' and not whatsapp_opt_in:
        return ''
    if preferred == 'text' and not sms_opt_in:
        return ''
    return preferred


def price_flag(service: str, shown: Any) -> str:
    """approved|review|unapproved|missing against APPROVED_PRICES (DESIGN §5.4); never computes a price."""
    if not isinstance(shown, dict) or shown.get('kind') not in c.PRICE_KINDS:
        return 'missing'
    amount = shown.get('amount_usd')
    if shown['kind'] in ('review', 'route_pending'):
        return 'review' if amount is None else 'unapproved'
    tier = shown.get('tier')
    approved = c.APPROVED_PRICES.get((service, tier)) if tier is None or isinstance(tier, str) else None
    if (approved and _is_int(amount) and amount == approved.amount_usd
            and _is_int(shown.get('per_document_usd')) and shown['per_document_usd'] == approved.per_document_usd
            and shown.get('plus_embassy_fee') is approved.plus_embassy_fee):
        return 'approved'
    return 'unapproved'


# --- Core hash and amendments (DESIGN §3.6) ---------------------------------------------------------

def hash_material(contact: dict, services: Iterable[tuple[str, dict]]) -> dict:
    """What the core hash covers: contact + services + answers + message (no timing, Turnstile or consent)."""
    deadline = contact.get('deadline')
    return {
        'contact': {
            'full_name': contact['full_name'].casefold(),
            'email': contact['email_normalized'],
            'phone': re.sub(r'\D', '', contact['phone_raw']),
            'preferred_channel': contact['preferred_channel'],
            'when': contact['when'],
            'deadline': deadline.isoformat() if deadline else None,
        },
        'message': contact['message'],
        'services': [{'service': key, 'answers': answers} for key, answers in services],
    }


def quote_material(quote) -> dict:
    """`hash_material` rebuilt from a saved QuoteSubmission and its lines."""
    contact = {field: getattr(quote, field) for field in (
        'full_name', 'email_normalized', 'phone_raw', 'preferred_channel', 'when', 'deadline', 'message',
    )}
    return hash_material(contact, [(line.service_key, line.answers) for line in quote.lines.order_by('position')])


def hash_of(material: dict) -> str:
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def changed_keys(old: dict, new: dict) -> list[str]:
    """`contact.phone`, `message`, `services`, `apostille.country`, … that differ between two materials."""
    changed = [f'contact.{key}' for key in new['contact'] if old['contact'].get(key) != new['contact'][key]]
    if old['message'] != new['message']:
        changed.append('message')
    old_services = {s['service']: s['answers'] for s in old['services']}
    new_services = {s['service']: s['answers'] for s in new['services']}
    if list(old_services) != list(new_services):
        changed.append('services')
    for service in new_services.keys() & old_services.keys():
        before, after = old_services[service], new_services[service]
        changed += sorted(f'{service}.{key}' for key in before.keys() | after.keys()
                          if before.get(key) != after.get(key))
    return changed


# --- Contact and consent (DESIGN §3.4) ---------------------------------------------------------------

def _contact(raw: Any, today: date, errors: dict, warnings: list) -> dict:
    data = raw if isinstance(raw, dict) else {}

    name = clean_text(data.get('full_name'))
    if len(name) < NAME_MIN:
        errors['contact.full_name'] = ['required']
    elif len(name) > NAME_MAX:
        errors['contact.full_name'] = ['too_long']

    email = clean_text(data.get('email'))
    if not email:
        errors['contact.email'] = ['required']
    elif len(email) > EMAIL_MAX or not _is_email(email):
        errors['contact.email'] = ['invalid']

    phone_raw = clean_text(data.get('phone'))
    if not phone_raw:
        errors['contact.phone'] = ['required']
    elif len(phone_raw) > PHONE_RAW_MAX or len(re.sub(r'\D', '', phone_raw)) < PHONE_MIN_DIGITS:
        errors['contact.phone'] = ['invalid']

    message = clean_text(data.get('message'), multiline=True)
    deadline = _deadline(data.get('deadline'), today, errors)
    if len(message) > MESSAGE_MAX:
        errors['contact.message'] = ['too_long']

    return {
        'full_name': name,
        'email': email,
        'email_normalized': email.lower(),
        'phone_raw': phone_raw,
        **parse_phone(phone_raw),
        'preferred_channel': _choice(data.get('preferred_channel'), c.CHANNELS, 'contact.preferred_channel',
                                     warnings),
        'when': _when(data.get('when'), warnings),
        'deadline': deadline,
        'message': message,
    }


def parse_phone(raw: str) -> dict:
    """E.164 when `phonenumbers` (region US) says valid; otherwise accepted with phone_valid=False (C15)."""
    e164 = ''
    try:
        number = phonenumbers.parse(raw, 'US')
        if phonenumbers.is_valid_number(number):
            e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    digits = re.sub(r'\D', '', e164 or raw)
    return {'phone_e164': e164, 'phone_valid': bool(e164), 'phone_last10': digits[-10:] if len(digits) >= 10 else ''}


def _is_email(value: str) -> bool:
    try:
        _validate_email(value)
    except ValidationError:
        return False
    return True


def _deadline(raw: Any, today: date, errors: dict) -> date | None:
    if raw in (None, ''):
        return None
    try:
        if not isinstance(raw, str) or not _DATE.match(raw):
            raise ValueError
        value = date.fromisoformat(raw)
    except ValueError:
        errors['contact.deadline'] = ['invalid']
        return None
    if value < today:
        errors['contact.deadline'] = ['past_date']
        return None
    return value


def _when(raw: Any, warnings: list) -> str:
    if raw in (None, ''):
        return ''
    if raw in c.WHEN_VALUES:
        return raw
    mapped = c.WHEN_ALIASES.get(str(raw).strip().lower())
    if mapped:
        return mapped
    warnings.append('contact.when:dropped')
    return ''


def _consent(raw: Any, preferred: str, warnings: list) -> dict:
    """Missing consent → statement terms and no opt-ins; an opt-in counts only for its own channel."""
    if not isinstance(raw, dict):
        warnings.append('consent:defaulted')
        raw = {}
    elif raw.get('terms_method') not in (None, c.TERMS_METHOD):
        warnings.append('consent.terms_method:defaulted')
    return {
        'terms_method': c.TERMS_METHOD,
        'terms_version': short_text(raw.get('terms_version'), 40),
        'consent_text_version': short_text(raw.get('consent_text_version'), 60),
        'whatsapp_opt_in': raw.get('whatsapp_opt_in') is True and preferred == 'whatsapp',
        'sms_opt_in': raw.get('sms_opt_in') is True and preferred == 'text',
    }


# --- Services and answers (DESIGN §3.2, §3.3) --------------------------------------------------------

def _lines(raw: Any, errors: dict, warnings: list) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        errors['services'] = ['required']
        return []
    if len(raw) > c.MAX_SERVICES:
        errors['services'] = ['too_long']
        return []
    lines, seen = [], set()
    for index, item in enumerate(raw):
        path = f'services[{index}]'
        if not isinstance(item, dict):
            errors[path] = ['invalid']
            continue
        key = item.get('service')
        if key in (None, ''):
            errors[f'{path}.service'] = ['required']
        elif not isinstance(key, str) or key not in c.SERVICE_KEYS:
            errors[f'{path}.service'] = ['invalid']
        elif key in seen:
            errors[f'{path}.service'] = ['duplicate']
        else:
            seen.add(key)
            lines.append(_line(path, key, item, warnings))
    if not errors:
        _order(lines, warnings)
        _derive(lines)
    return lines


def _line(path: str, key: str, item: dict, warnings: list) -> dict:
    source = item.get('source')
    if source not in c.LINE_SOURCES:
        warnings.append(f'{path}.source:defaulted')
        source = 'user'
    answers, dropped, review = normalise_answers(key, item.get('answers'), path, warnings)
    route = _route(item.get('route'), f'{path}.route', warnings)
    shown = item.get('price_shown') if isinstance(item.get('price_shown'), dict) else None
    if (route and route['status'] == 'pending') or (shown or {}).get('kind') == 'route_pending':
        review.append('route_pending')
    return {
        'position': item.get('position'),     # the client's value; _order() sets the final one
        'service_key': key,
        'source': source,
        'addon_of': _paired(item.get('addon_of'), source == 'addon', f'{path}.addon_of', warnings),
        'switched_from': _paired(item.get('switched_from'), source == 'route_switch', f'{path}.switched_from',
                                 warnings),
        'answers': answers,
        'answers_raw': item.get('answers') if isinstance(item.get('answers'), dict) else {},
        'answer_source': _answer_source(key, item.get('answer_source')),
        'dropped_answers': dropped,
        'addons': _addons(key, item.get('addons'), f'{path}.addons', warnings),
        'route': route,
        'price_shown': shown,
        'price_shown_text': short_text((shown or {}).get('text'), 200),
        'price_kind': shown['kind'] if shown and shown.get('kind') in c.PRICE_KINDS else '',
        'price_flag': price_flag(key, shown),
        'summary_client': short_text(item.get('summary'), 300),
        'implied_services': [],
        'needs_review': bool(review),
        'review_reasons': review,
        'is_primary': False,
    }


def normalise_answers(service: str, raw: Any, path: str, warnings: list) -> tuple[dict, dict, list[str]]:
    """(answers, dropped_answers {key: reason}, review_reasons) for one line; never raises."""
    spec = c.ANSWERS[service]
    if raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        warnings.append(f'{path}.answers:dropped')
        raw = {}
    answers, dropped = {}, {}
    for key, value in raw.items():
        answer = spec.get(key)
        if answer is None:
            dropped[key] = 'unknown_key'
            continue
        normalised, reason = _answer_value(answer, value)
        if normalised is not None:
            answers[key] = normalised
        if reason:
            dropped[key] = reason
    if service == 'apostille':           # the 03 §6.4 conditional prune, re-applied on the server
        doc = answers.get('doc')
        for key, shown in (('state', doc in c.STATE_DOCS), ('fbi_pdf', doc == 'fbi_report')):
            if key in answers and not shown:
                del answers[key]
                dropped[key] = 'hidden'
    review = []
    if service == 'translation' and answers.get('from') and answers.get('from') == answers.get('to') \
            and answers['from'] != 'other':
        review.append('same_language')
    if service == 'ron' and answers.get('doc') == 'will_trust':
        review.append('will_trust_online')
    return answers, dropped, review


def _answer_value(answer: c.Answer, value: Any) -> tuple[Any, str]:
    """(normalised value or None, reason or ''); None/'' is an unanswered question, not an error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ''
    if answer.kind == c.ENUM:
        text = value.strip() if isinstance(value, str) else None
        if text in answer.values:
            return text, ''
        if text and text.lower() in answer.values:
            return text.lower(), ''
        return None, 'invalid_enum'
    if answer.kind == c.COUNT:
        count = _as_int(value)
        return (count, '') if count is not None and c.COUNT_MIN <= count <= c.COUNT_MAX else (None, 'invalid_count')
    if answer.kind in (c.COUNTRY, c.LANGUAGE):
        lookup = c.country_lookup() if answer.kind == c.COUNTRY else c.language_lookup()
        found = lookup.get(c.fold(value)) if isinstance(value, str) else None
        return (found, '') if found else (None, 'invalid_enum')
    if answer.kind == c.US_STATE:
        text = value.strip() if isinstance(value, str) else ''
        if text.upper() in c.US_STATES:
            return text.upper(), ''
        return (c.STATE_UNSURE, '') if text.lower() == c.STATE_UNSURE else (None, 'invalid_enum')
    if answer.kind == c.ZIP:
        text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
        return (text, '') if _ZIP.match(text) else (None, 'invalid_zip')
    if answer.kind == c.TEXT:
        text = clean_text(value, multiline=True)
        if not text:
            return None, ''
        return (text[:c.TEXT_MAX], 'truncated') if len(text) > c.TEXT_MAX else (text, '')
    raise ValueError(f'unknown answer kind {answer.kind}')


def _order(lines: list[dict], warnings: list) -> None:
    """Sort by the client's positions when they are valid and unique, else keep array order; renumber 1..n."""
    positions = [line['position'] for line in lines]
    valid = all(_is_int(p) and 1 <= p <= c.MAX_SERVICES for p in positions) and len(set(positions)) == len(lines)
    if valid:
        lines.sort(key=lambda line: line['position'])
    renumbered = False
    for number, line in enumerate(lines, start=1):
        renumbered = renumbered or line['position'] != number
        line['position'] = number
    if renumbered:
        warnings.append('services:positions_recomputed')


def _derive(lines: list[dict]) -> None:
    """Implied services and the primary line (DESIGN §5.4)."""
    keys = {line['service_key'] for line in lines}
    for line in lines:
        for (service, answer_key), (implied, _reason) in c.IMPLIED_BY.items():
            if line['service_key'] == service and line['answers'].get(answer_key) == 'yes' and implied not in keys:
                line['implied_services'].append(implied)
    primary = next((line for line in lines if line['source'] in c.PRIMARY_SOURCES), None)
    if primary is None:
        primary = next((line for line in lines if line['source'] != 'addon'), lines[0])
    primary['is_primary'] = True


def _paired(value: Any, allowed: bool, path: str, warnings: list) -> str:
    """`addon_of` only with source=addon, `switched_from` only with source=route_switch."""
    if value in (None, ''):
        return ''
    if allowed and value in c.ADDON_PARENTS:
        return value
    warnings.append(f'{path}:dropped')
    return ''


def _addons(service: str, raw: Any, path: str, warnings: list) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    kept = [] if service not in c.ADDON_PARENTS else list(dict.fromkeys(a for a in items if a in c.ADDON_SERVICES))
    if len(kept) != len(items):
        warnings.append(f'{path}:dropped')
    return kept


def _answer_source(service: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key in c.ANSWERS[service] and value in c.ANSWER_SOURCES}


def _route(raw: Any, path: str, warnings: list) -> dict | None:
    if raw is None:
        return None
    data = raw if isinstance(raw, dict) else {}
    country = c.country_lookup().get(c.fold(data['country'])) if isinstance(data.get('country'), str) else None
    if not (country and data.get('expected') in c.ROUTE_EXPECTED and data.get('status') in c.ROUTE_STATUSES):
        warnings.append(f'{path}:dropped')
        return None
    return {'country': country, 'expected': data['expected'], 'status': data['status'],
            'clash': data.get('clash') is True}


# --- Request metadata fields -------------------------------------------------------------------------

def _submission_id(raw: Any, errors: dict) -> uuid.UUID | None:
    if raw in (None, ''):
        errors['submission_id'] = ['required']
        return None
    parsed = parse_uuid(raw)
    if parsed is None:
        errors['submission_id'] = ['invalid']
    return parsed


def parse_uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if isinstance(raw, str) else None
    except ValueError:
        return None


def _form_version(raw: Any, warnings: list) -> str:
    if not isinstance(raw, str) or not raw.strip():
        warnings.append('form_version:defaulted')
        return 'unknown'
    if len(raw.strip()) > 40:
        warnings.append('form_version:truncated')
    return raw.strip()[:40]


def _locale(raw: Any, warnings: list) -> str:
    if raw in c.LOCALES:
        return raw
    warnings.append('locale:defaulted')
    return 'en'


def _choice(raw: Any, allowed: tuple[str, ...], path: str, warnings: list) -> str:
    if raw in (None, ''):
        return ''
    if raw in allowed:
        return raw
    warnings.append(f'{path}:dropped')
    return ''


def _page(raw: Any, warnings: list) -> dict:
    if not isinstance(raw, dict):
        warnings.append('page:missing')
        raw = {}
    url = raw.get('url')
    if url not in (None, '') and not (isinstance(url, str) and urlsplit(url.strip()).scheme in ('http', 'https')):
        warnings.append('page.url:dropped')
        url = ''
    return {
        'page_url': short_text(url, 1000),
        'page_path': short_text(raw.get('path'), 255),
        'referrer_path': short_text(raw.get('referrer_path'), 255),
    }


def _entry(raw: Any) -> dict:
    """Whitelisted keys only; values ≤100 (report 03 §6.3)."""
    if not isinstance(raw, dict):
        return {}
    params = raw.get('params') if isinstance(raw.get('params'), dict) else {}
    lists = {key: raw.get(key) if isinstance(raw.get(key), list) else [] for key in ('preselected', 'bad_params')}
    return {
        'type': raw.get('type') if raw.get('type') in ENTRY_TYPES else None,
        'params': {key: short_text(params.get(key), 100) or None for key in ENTRY_PARAMS},
        'preselected': list(dict.fromkeys(k for k in lists['preselected'] if k in c.SERVICE_KEYS)),
        'bad_params': [short_text(name, 40) for name in lists['bad_params'] if isinstance(name, str)][:20],
        'cta_id': short_text(raw.get('cta_id'), 100) or None,
        'cta_page': short_text(raw.get('cta_page'), 255) or None,
    }


def _timing(raw: Any, warnings: list) -> dict:
    """Missing timing is a suspicion signal (row 12), never a rejection."""
    data = raw if isinstance(raw, dict) else {}
    elapsed = data.get('elapsed_ms')
    if not (_is_int(elapsed) and elapsed >= 0):
        warnings.append('timing:missing')
        elapsed = None
    return {
        'started_at': short_text(data.get('started_at'), 40) or None,
        'submitted_at': short_text(data.get('submitted_at'), 40) or None,
        'elapsed_ms': elapsed,
    }


def _client(raw: Any) -> dict:
    data = raw if isinstance(raw, dict) else {}
    viewport = data.get('viewport_w')
    return {
        'tz': short_text(data.get('tz'), 64) or None,
        'viewport_w': viewport if _is_int(viewport) and viewport >= 0 else None,
        'ga_client_id': short_text(data.get('ga_client_id'), 64) or None,
    }


def _price_notes(raw: Any) -> list[str]:
    return list(dict.fromkeys(n for n in raw if n in c.PRICE_NOTES)) if isinstance(raw, list) else []


def _files_dropped(raw: Any, warnings: list) -> bool:
    """Uploads are off (WP9): a non-empty `files` is ignored, never a 400."""
    if raw in (None, []):
        return False
    warnings.append('files:dropped')
    return True


# --- Text helpers ----------------------------------------------------------------------------------

def clean_text(value: Any, *, multiline: bool = False) -> str:
    """NFC, control characters removed (newlines and tabs kept when multiline), trimmed."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ''
    text = unicodedata.normalize('NFC', str(value))
    return (_CONTROL if multiline else _CONTROL_SINGLE_LINE).sub('', text).strip()


def short_text(value: Any, limit: int) -> str:
    return clean_text(value)[:limit]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(value: Any) -> int | None:
    if _is_int(value):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
