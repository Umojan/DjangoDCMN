"""Zoho CRM transport for the new code (DESIGN §7.1, §7.8; invariant 4).

Every Zoho call of `quotes` and `crm` goes through `zoho_call`: the OAuth token (the legacy cache entry via
`orders.zoho_sync.get_access_token`), error classification, timeouts and the circuit breaker live here.
Writes send `"trigger": []` unless the caller passes a per-module opt-in.

Logs and exception texts carry the method, the path (module and record ids), the HTTP status and Zoho's
code and field name only: never query params, bodies or Zoho's free-text messages (invariant 9).
"""

import logging
import time
from dataclasses import dataclass
from typing import Sequence

import requests
from django.conf import settings
from django.core.cache import cache

from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

from . import flags

logger = logging.getLogger(__name__)

ZOHO_API_DOMAIN = 'https://www.zohoapis.com'
API_BASES = {'v2': f'{ZOHO_API_DOMAIN}/crm/v2', 'v6': f'{ZOHO_API_DOMAIN}/crm/v6'}
ZOHO_WEB_BASE = 'https://crm.zoho.com/crm'
KEY_FIELD = 'External_Order_Key'

Timeout = tuple[float, float]
DEFAULT_TIMEOUT: Timeout = (5, 15)
RECORD_WRITES = frozenset({'POST', 'PUT', 'PATCH'})

# Circuit breaker, shared by intake and engine: 5 transient errors within 120 s open it for 60 s.
CIRCUIT_KEY = 'crm:zoho:circuit'
CIRCUIT_FAILURES_KEY = 'crm:zoho:circuit:failures'
CIRCUIT_THRESHOLD = 5
CIRCUIT_WINDOW_S = 120
CIRCUIT_OPEN_S = 60

# Inline budget (DESIGN §7.8 step 7): each call gets (1.0, min(4.0, remaining - 1.5)); none below 2.5 s.
INLINE_CONNECT_S = 1.0
INLINE_READ_MAX_S = 4.0
INLINE_MARGIN_S = 1.5
INLINE_MIN_REMAINING_S = 2.5

# The OAuth token: the legacy cache entry of orders.zoho_sync.get_access_token (same key and TTL). A call that
# must answer fast (read timeout ≤ INLINE_READ_MAX_S: the inline budget, a button's source read) refreshes it
# within its own timeout, never the legacy (5, 45), and does not retry a 401 with a second refresh.
TOKEN_CACHE_KEY = 'zoho_access_token'
TOKEN_TTL_S = 2900
TOKEN_URL = 'https://accounts.zoho.com/oauth/v2/token'
LEGACY_TOKEN_TIMEOUT: 'Timeout' = (5, 45)   # orders.zoho_sync.ZOHO_REQUEST_TIMEOUT, for background refreshes
# Zoho allows 10 token requests per 10 minutes per refresh token and answers the next ones with HTTP 200
# {"error": "Access Denied", "error_description": "You have made too many requests continuously…"} (seen live
# 2026-11-27). That is transient: no worker asks again until the window is over, and the callers retry.
TOKEN_COOLDOWN_KEY = 'crm:zoho:token_cooldown'
TOKEN_COOLDOWN_S = 600
# A 401 no new token can fix (the prod token has no ZohoCRM.org.READ, 06-facts) never costs a refresh; every
# other 401 refreshes once, as before.
NO_REFRESH_401_CODES = frozenset({'OAUTH_SCOPE_MISMATCH'})


class _ZohoErrorInfo:
    """Zoho's error code, the field it names and the HTTP status (for `last_error_code` and messages)."""

    def __init__(self, message: str, *, code: str = '', field: str = '', http_status: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.http_status = http_status
        self.details = details or {}

    @property
    def duplicate_id(self) -> str:
        """Id of the existing record a DUPLICATE_DATA error points at, if Zoho sent it."""
        duplicate = self.details.get('duplicate_record')
        return str(self.details.get('id') or (duplicate.get('id') if isinstance(duplicate, dict) else '') or '')


class ZohoTransient(_ZohoErrorInfo, ZohoTransientError):
    """Retry later: timeout, connection error, 429, 5xx, a token refresh failure or an invalid token."""


class ZohoPermanent(_ZohoErrorInfo, ZohoPermanentError):
    """Needs a human: INVALID_DATA, MANDATORY_NOT_FOUND, DUPLICATE_DATA on another field, …"""


class CircuitOpen(ZohoTransient):
    """Zoho failed repeatedly; no HTTP call is made until the circuit closes."""


class TokenCooldown(CircuitOpen):
    """Zoho throttled the token refresh a moment ago; no HTTP call is made until TOKEN_COOLDOWN_S is over."""


@dataclass
class CreateResult:
    id: str
    created: bool  # False: DUPLICATE_DATA on the key resolved an existing record (ours, created earlier)


# --- Transport ----------------------------------------------------------------------------------

def zoho_call(method: str, path: str, *, params: dict | None = None, json: dict | None = None,
              api: str = 'v2', timeout: Timeout = DEFAULT_TIMEOUT) -> dict | None:
    """One Zoho request: the parsed JSON body, None on 204.

    Raises CircuitOpen (no HTTP) while the circuit is open, ZohoTransient or ZohoPermanent otherwise.
    """
    method = method.upper()
    path = path.lstrip('/')
    if method in RECORD_WRITES and api == 'v2' and 'trigger' not in (json or {}):
        raise ValueError(f'{method} {path}: a record write must send "trigger" (invariant 4)')
    if circuit_is_open():
        raise CircuitOpen(f'{method} {path}: circuit open', code='CIRCUIT_OPEN')
    url = f'{API_BASES[api]}/{path}'
    try:
        response = _send(method, url, path, params=params, json=json, timeout=timeout)
        if response.status_code == 401 and _error_code(response) not in NO_REFRESH_401_CODES:
            if _is_fast(timeout):   # no second refresh inside a short budget: the next call refreshes
                _forget_token(response.request_token)
            else:
                response = _send(method, url, path, params=params, json=json, timeout=timeout, force_refresh=True)
        body = _parse(method, path, response)
    except CircuitOpen as exc:   # the token cooldown: no HTTP was made, so not a Zoho failure
        logger.info('zoho_no_call method=%s path=%s code=%s', method, path, exc.code)
        raise
    except ZohoTransientError as exc:
        logger.warning('zoho_transient method=%s path=%s code=%s', method, path, getattr(exc, 'code', ''))
        _record_failure()
        raise
    except ZohoPermanentError:
        _record_success()
        raise
    _record_success()
    return body


def _is_fast(timeout: Timeout) -> bool:
    return timeout[1] <= INLINE_READ_MAX_S


def _send(method: str, url: str, path: str, *, params, json, timeout: Timeout,
          force_refresh: bool = False) -> requests.Response:
    # A fast call refreshes within its own timeout; a background one with the legacy (5, 45).
    refresh_timeout = timeout if _is_fast(timeout) else LEGACY_TOKEN_TIMEOUT
    try:
        token = (None if force_refresh else cache.get(TOKEN_CACHE_KEY)) or refresh_token(refresh_timeout)
    except TokenCooldown:
        raise
    except ZohoTransientError as exc:
        raise ZohoTransient(f'{method} {path}: token refresh failed',
                            code=getattr(exc, 'code', '') or 'TOKEN_REFRESH') from exc
    except ZohoPermanentError as exc:
        raise ZohoPermanent(f'{method} {path}: token refresh failed', code='TOKEN_REFRESH') from exc
    headers = {'Authorization': f'Zoho-oauthtoken {token}'}
    try:
        response = requests.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
    except requests.Timeout as exc:
        raise ZohoTransient(f'{method} {path}: timeout', code='TIMEOUT') from exc
    except requests.RequestException as exc:
        raise ZohoTransient(f'{method} {path}: {type(exc).__name__}', code='NETWORK') from exc
    response.request_token = token
    return response


def refresh_token(timeout: Timeout) -> str:
    """A new OAuth access token within `timeout`, stored where the legacy code reads it.

    Mirrors orders.zoho_sync.get_access_token(force_refresh=True) with the caller's timeout, except that a
    throttled refresh is transient (`TOKEN_RATE_LIMIT`) and pauses every refresh for TOKEN_COOLDOWN_S.
    """
    if cache.get(TOKEN_COOLDOWN_KEY):
        raise TokenCooldown('token refresh paused: Zoho throttled it', code='TOKEN_RATE_LIMIT')
    params = {'refresh_token': settings.ZOHO_REFRESH_TOKEN, 'client_id': settings.ZOHO_CLIENT_ID,
              'client_secret': settings.ZOHO_CLIENT_SECRET, 'grant_type': 'refresh_token'}
    try:
        response = requests.post(TOKEN_URL, params=params, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ZohoTransient(f'token refresh failed: {type(exc).__name__}', code='TOKEN_REFRESH') from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    token = body.get('access_token')
    if response.ok and isinstance(token, str) and token:
        cache.set(TOKEN_CACHE_KEY, token, TOKEN_TTL_S)
        logger.info('zoho_token_refreshed read_timeout=%s', timeout[1])
        return token
    if _is_throttled(body):
        cache.set(TOKEN_COOLDOWN_KEY, 1, TOKEN_COOLDOWN_S)
        logger.warning('zoho_token_throttled http=%s cooldown_s=%s', response.status_code, TOKEN_COOLDOWN_S)
        raise ZohoTransient('token refresh throttled by Zoho', code='TOKEN_RATE_LIMIT',
                            http_status=response.status_code)
    if not response.ok:
        raise ZohoTransient(f'token refresh failed: HTTP {response.status_code}', code='TOKEN_REFRESH',
                            http_status=response.status_code)
    logger.error('zoho_token_refresh_refused error=%s', str(body.get('error') or 'no access_token')[:40])
    raise ZohoPermanent('token refresh answered without access_token', code='TOKEN_REFRESH')


def _is_throttled(body: dict) -> bool:
    """Zoho's answer to an 11th token request inside 10 minutes (HTTP 200, no access_token)."""
    error = str(body.get('error') or '').strip().casefold()
    description = str(body.get('error_description') or '').casefold()
    return error == 'access denied' or 'too many requests' in description


def token_cooling_down() -> bool:
    return bool(cache.get(TOKEN_COOLDOWN_KEY))


def _error_code(response) -> str:
    """The upper-case Zoho error code of a response, '' when it has none."""
    try:
        body = response.json()
    except ValueError:
        return ''
    return str(_first_item(body).get('code') or '').upper()


def _forget_token(token: str | None) -> None:
    """Drop a token Zoho refused (only if no other worker has replaced it meanwhile)."""
    if token and cache.get(TOKEN_CACHE_KEY) == token:
        cache.delete(TOKEN_CACHE_KEY)


def _parse(method: str, path: str, response) -> dict | None:
    status = response.status_code
    if status == 204:
        return None
    try:
        body = response.json()
    except ValueError:
        body = None
    item = _first_item(body)
    code = str(item.get('code') or '').upper()
    if status == 401:
        error = ZohoPermanent if code == 'OAUTH_SCOPE_MISMATCH' else ZohoTransient
        raise error(f'{method} {path}: HTTP 401 {code or "INVALID_TOKEN"}', code=code or 'INVALID_TOKEN',
                    http_status=status)
    if status == 429 or status >= 500:
        raise ZohoTransient(f'{method} {path}: HTTP {status} {code}'.rstrip(), code=code or f'HTTP_{status}',
                            http_status=status)
    if status >= 400:
        raise _item_error(method, path, item, status)
    if not isinstance(body, dict):
        raise ZohoPermanent(f'{method} {path}: HTTP {status} without a JSON object', code='INVALID_JSON',
                            http_status=status)
    return body


def _first_item(body) -> dict:
    """The first per-record result (`data[0]`, or `fields[0]` of the Settings API), or a top-level error."""
    if not isinstance(body, dict):
        return {}
    for name in ('data', 'fields'):
        items = body.get(name)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return body if 'code' in body else {}


def _item_error(method: str, path: str, item: dict, status: int | None) -> ZohoPermanent:
    details = item.get('details') if isinstance(item.get('details'), dict) else {}
    field = str(details.get('api_name') or '')
    code = str(item.get('code') or (f'HTTP_{status}' if status else 'NO_RESULT')).upper()
    message = f'{method} {path}: {code}' + (f' ({field})' if field else '')
    return ZohoPermanent(message, code=code, field=field, http_status=status, details=details)


def result_id(body: dict | None, method: str, path: str) -> str:
    """Id of the first record of a write response; raises ZohoPermanent when Zoho rejected it."""
    item = _first_item(body)
    details = item.get('details') if isinstance(item.get('details'), dict) else {}
    succeeded = str(item.get('code') or '').upper() == 'SUCCESS' or str(item.get('status') or '').lower() == 'success'
    if succeeded and details.get('id'):
        return str(details['id'])
    raise _item_error(method, path, item, None)


# --- Circuit breaker ----------------------------------------------------------------------------

def circuit_is_open() -> bool:
    open_until = cache.get(CIRCUIT_KEY)
    return bool(open_until) and time.time() < open_until


def _record_failure() -> None:
    now = time.time()
    recent = [t for t in (cache.get(CIRCUIT_FAILURES_KEY) or []) if now - t < CIRCUIT_WINDOW_S]
    recent.append(now)
    if len(recent) >= CIRCUIT_THRESHOLD:
        cache.set(CIRCUIT_KEY, now + CIRCUIT_OPEN_S, CIRCUIT_OPEN_S)
        cache.delete(CIRCUIT_FAILURES_KEY)
        logger.warning('zoho_circuit_open failures=%s open_s=%s', len(recent), CIRCUIT_OPEN_S)
    else:
        cache.set(CIRCUIT_FAILURES_KEY, recent, CIRCUIT_WINDOW_S)


def _record_success() -> None:
    # Zoho answered: the transient errors are no longer consecutive.
    cache.delete(CIRCUIT_FAILURES_KEY)


def budget_timeout(deadline: float | None, default: Timeout = DEFAULT_TIMEOUT) -> Timeout | None:
    """Timeout for the next call of an inline request; `deadline` is a time.monotonic() value.

    None means less than 2.5 s is left: the caller must not start the call and goes async instead.
    """
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining < INLINE_MIN_REMAINING_S:
        return None
    return INLINE_CONNECT_S, min(INLINE_READ_MAX_S, remaining - INLINE_MARGIN_S)


# --- Records ------------------------------------------------------------------------------------

def create_once(module: str, record: dict, key: str, *, trigger: Sequence[str] = (),
                timeout: Timeout = DEFAULT_TIMEOUT) -> CreateResult:
    """Create a record keyed by External_Order_Key; a retry can never duplicate it.

    Always POSTs (no search first: a search miss right after a create proves nothing). DUPLICATE_DATA on the
    key → the existing id from the error details, else a trigger-free upsert that changes no business field.
    """
    payload = {'data': [{**record, KEY_FIELD: key}], 'trigger': list(trigger)}
    try:
        body = zoho_call('POST', module, json=payload, timeout=timeout)
        return CreateResult(id=result_id(body, 'POST', module), created=True)
    except ZohoPermanent as exc:
        # Zoho names the duplicated field in details.api_name; an answer without it is treated as our key
        # (the upsert-resolve then matches by key or fails, it never overwrites another record).
        if exc.code != 'DUPLICATE_DATA' or exc.field not in ('', KEY_FIELD):
            raise
        duplicate_id = exc.duplicate_id
    record_id = duplicate_id or _resolve_by_key(module, key, timeout)
    logger.info('zoho_duplicate_resolved module=%s id=%s', module, record_id)
    return CreateResult(id=record_id, created=False)


def _resolve_by_key(module: str, key: str, timeout: Timeout) -> str:
    path = f'{module}/upsert'
    body = zoho_call('POST', path, json={
        'data': [{KEY_FIELD: key}],
        'duplicate_check_fields': [KEY_FIELD],
        'trigger': [],
    }, timeout=timeout)
    record_id = result_id(body, 'POST', path)
    if str(_first_item(body).get('action') or '').lower() == 'insert':
        logger.warning('zoho_upsert_resolve_inserted module=%s id=%s', module, record_id)
    return record_id


def update(module: str, record_id: str, fields: dict, *, trigger: Sequence[str] = (),
           timeout: Timeout = DEFAULT_TIMEOUT) -> None:
    body = zoho_call('PUT', module, json={'data': [{'id': record_id, **fields}], 'trigger': list(trigger)},
                     timeout=timeout)
    result_id(body, 'PUT', module)


def get(module: str, record_id: str, *, fields: list[str] | None = None,
        timeout: Timeout = DEFAULT_TIMEOUT) -> dict | None:
    """The record, or None when Zoho answers 204/404 (deleted or never existed)."""
    params = {'fields': ','.join(fields)} if fields else None
    try:
        body = zoho_call('GET', f'{module}/{record_id}', params=params, timeout=timeout)
    except ZohoPermanent as exc:
        if exc.http_status == 404:
            return None
        raise
    records = (body or {}).get('data') or []
    return records[0] if records else None


def search(module: str, criteria: str, *, fields: list[str] | None = None, per_page: int = 10,
           timeout: Timeout = DEFAULT_TIMEOUT) -> list[dict]:
    """Records matching a Zoho criteria string; [] on 204. Build values with `criteria_value`."""
    params = {'criteria': criteria, 'per_page': per_page}
    if fields:
        params['fields'] = ','.join(fields)
    body = zoho_call('GET', f'{module}/search', params=params, timeout=timeout)
    return list((body or {}).get('data') or [])


def search_by_key(module: str, key: str, *, timeout: Timeout = DEFAULT_TIMEOUT) -> dict | None:
    """The record holding our key. A miss is not proof of absence: the search index may lag."""
    rows = search(module, f'({KEY_FIELD}:equals:{criteria_value(key)})', per_page=2, timeout=timeout)
    return rows[0] if rows else None


def criteria_value(value: str) -> str:
    """Escape a value for Zoho search criteria (backslash before `\\`, `(`, `)` and `,`)."""
    return ''.join(f'\\{ch}' if ch in '\\(),' else ch for ch in str(value))


def timeline(module: str, record_id: str, *, max_pages: int = 3,
             timeout: Timeout = DEFAULT_TIMEOUT) -> list[dict]:
    """Timeline events of a record (v6 `__timeline`), following page tokens for up to `max_pages`."""
    events: list[dict] = []
    params: dict | None = None
    for _ in range(max_pages):
        body = zoho_call('GET', f'{module}/{record_id}/__timeline', params=params, api='v6', timeout=timeout) or {}
        events.extend(body.get('__timeline') or [])
        info = body.get('info') or {}
        if not (info.get('more_records') and info.get('next_page_token')):
            break
        params = {'page_token': info['next_page_token']}
    return events


def add_note(module: str, record_id: str, title: str, content: str, *,
             timeout: Timeout = DEFAULT_TIMEOUT) -> str:
    """Create a Note on a record; returns the note id."""
    body = zoho_call('POST', 'Notes', json={
        'data': [{'Note_Title': title, 'Note_Content': content, 'Parent_Id': record_id, 'se_module': module}],
        'trigger': [],
    }, timeout=timeout)
    return result_id(body, 'POST', 'Notes')


def list_notes(module: str, record_id: str, *, timeout: Timeout = DEFAULT_TIMEOUT) -> list[dict]:
    body = zoho_call('GET', f'{module}/{record_id}/Notes', params={'per_page': 200}, timeout=timeout)
    return list((body or {}).get('data') or [])


def add_tags(module: str, record_id: str, tags: list[str], *, timeout: Timeout = DEFAULT_TIMEOUT) -> bool:
    """Attach existing tags (`over_write=false`). Never creates a tag: the tag is made by hand (ZOHO-SETUP Z4.5).

    A rejected call (e.g. a missing tag) logs `tag_missing` and returns False; transient errors propagate.
    """
    if not tags:
        return False
    path = f'{module}/{record_id}/actions/add_tags'
    try:
        body = zoho_call('POST', path, params={'tag_names': ','.join(tags), 'over_write': 'false'},
                         json={'trigger': []}, timeout=timeout)
        result_id(body, 'POST', path)
    except ZohoPermanent as exc:
        logger.warning('tag_missing module=%s id=%s code=%s', module, record_id, exc.code)
        return False
    return True


def fields_metadata(module: str, *, timeout: Timeout = DEFAULT_TIMEOUT) -> list[dict]:
    """Field metadata of a module (v6 `settings/fields`)."""
    body = zoho_call('GET', 'settings/fields', params={'module': module}, api='v6', timeout=timeout)
    return list((body or {}).get('fields') or [])


def record_url(tab: str, record_id: str) -> str:
    """Zoho web URL of a record, e.g. …/tab/CustomModule2/<id> (DESIGN §7.4, A10)."""
    return f'{ZOHO_WEB_BASE}/{flags.org_id()}/tab/{tab}/{record_id}'
