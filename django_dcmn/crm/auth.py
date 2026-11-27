"""Engine API auth (DESIGN §8.1): the `X-DCMN-Key` header against CRM_ACTION_KEYS, caller scopes, throttle.

Every configured key is compared with `hmac.compare_digest`, with no early exit; the matching caller is taken
at the end. A key, token or api_key sent in the body or the query string is ignored and counted
(`body_token_ignored`), never logged with its value. `request.auth` is the caller key id: the first 8 hex
chars of sha256(key). The key itself is never stored or logged.
"""

import hashlib
import hmac
import logging

from django.core.cache import cache
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, ParseError, UnsupportedMediaType
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from quotes.render import ET

from . import flags

logger = logging.getLogger(__name__)

HEADER = 'X-DCMN-Key'
NEXT_SUFFIX = '-next'
TOKEN_FIELDS = ('key', 'token', 'api_key')
IGNORED_COUNTER = 'crm:auth:body_token_ignored:{day}'
IGNORED_COUNTER_TTL_S = 8 * 24 * 3600


class CallerUser:
    """The caller a key authenticates (`zoho`, `gmail`, `rules`); not a Django user."""

    is_authenticated = True
    is_anonymous = False

    def __init__(self, caller: str):
        self.caller = caller

    def __str__(self):
        return self.caller


def parse_keys(raw: str) -> list[tuple[str, bytes]]:
    """`"zoho:k,zoho-next:k2"` → `[("zoho", b"k"), ("zoho", b"k2")]`; entries without a caller or key are skipped."""
    keys = []
    for entry in (raw or '').split(','):
        label, _, key = entry.strip().partition(':')
        label, key = label.strip(), key.strip()
        if label and key:
            keys.append((label.removesuffix(NEXT_SUFFIX), key.encode('utf-8')))
    return keys


def key_id(key: bytes) -> str:
    """What the audit keeps of a key: sha256(key)[:8]."""
    return hashlib.sha256(key).hexdigest()[:8]


def match_key(presented: str) -> tuple[str, bytes] | None:
    """The (caller, key) whose key equals `presented`; every configured key is compared, no early exit."""
    given = presented.encode('utf-8')
    found = None
    for caller, key in parse_keys(flags.action_keys_raw()):
        if hmac.compare_digest(given, key):
            found = (caller, key)
    return found


def _counter(day) -> str:
    return IGNORED_COUNTER.format(day=day.isoformat())


def ignored_tokens_on(day) -> int:
    """How many requests of that ET day carried a key-like field in the body or the query (digest line)."""
    return int(cache.get(_counter(day)) or 0)


def _count_ignored_tokens(request) -> None:
    """A key-like field in the query string or the body is never used; count it (no value is logged)."""
    places = [name for name in TOKEN_FIELDS if name in request.query_params]
    try:
        data = request.data
    except (ParseError, UnsupportedMediaType):   # the view answers the bad body itself
        data = None
    if isinstance(data, dict):
        places += [name for name in TOKEN_FIELDS if name in data]
    if not places:
        return
    counter = _counter(timezone.now().astimezone(ET).date())
    if not cache.add(counter, 1, IGNORED_COUNTER_TTL_S):
        try:
            cache.incr(counter)
        except ValueError:
            cache.add(counter, 1, IGNORED_COUNTER_TTL_S)
    logger.warning('body_token_ignored fields=%s', ','.join(sorted(set(places))))


class ActionKeyAuthentication(BaseAuthentication):
    """Header `X-DCMN-Key` only. Returns (CallerUser(caller), caller_key_id); a missing header → 401."""

    def authenticate(self, request):
        _count_ignored_tokens(request)
        presented = request.headers.get(HEADER, '')
        if not presented:
            return None
        found = match_key(presented)
        if found is None:
            logger.warning('crm_key_refused reason=wrong_key')
            raise AuthenticationFailed('unauthorized')
        caller, key = found
        return CallerUser(caller), key_id(key)

    def authenticate_header(self, request):
        return HEADER


class CallerScope(BasePermission):
    """`view.allowed_callers = {"POST": {"zoho"}, …}` (DESIGN §8.1 caller table)."""

    message = 'scope'

    def has_permission(self, request, view):
        caller = getattr(request.user, 'caller', None)
        return bool(caller) and caller in getattr(view, 'allowed_callers', {}).get(request.method, ())


class CallerKeyThrottle(SimpleRateThrottle):
    """`crm_key`: 120/min per caller."""

    scope = 'crm_key'

    def get_cache_key(self, request, view):
        caller = getattr(request.user, 'caller', None)
        return self.cache_format % {'scope': self.scope, 'ident': caller} if caller else None
