"""Panel links and the panel cookie (DESIGN §8.3, §8.4).

A minted link carries `TimestampSigner(salt="crm.panel").sign_object({m, r, a, act, jti})`: valid 900 s and
single-use (`cache.add("crm:panel:jti:{jti}", 1, 900)`). `act` is always `add_service`. The token is part of a
URL, so it carries ids only: `a` is the Zoho user id, never the login email (URLs reach access logs).

Opening the panel moves `{m, r, a, sid, initials}` into the separate cookie `crm_panel`, signed with
`django.core.signing` (salt `crm.panel.cookie`, 1800 s) and set with path `/crm/panel/`, HttpOnly, Secure and
SameSite=Strict. The Django session is not used, so admin sessions are unaffected.

The cookie is one per browser, so every rendered panel also carries `panel_ref` (signed {m, r, sid}, salt
`crm.panel.form`) in its forms and poll URL: an action from a window whose record is no longer the cookie's
(another Add service… panel was opened since) is refused, never run on the other record.
"""

import uuid

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.urls import reverse

TOKEN_SALT = 'crm.panel'
TOKEN_MAX_AGE_S = 900
ACTION = 'add_service'
JTI_KEY = 'crm:panel:jti:{jti}'

COOKIE_NAME = 'crm_panel'
COOKIE_SALT = 'crm.panel.cookie'
COOKIE_PATH = '/crm/panel/'
COOKIE_MAX_AGE_S = 1800
FORM_SALT = 'crm.panel.form'

__all__ = ['BadSignature', 'SignatureExpired', 'TokenReused', 'make_panel_url', 'read_panel_token',
           'read_panel_cookie', 'set_panel_cookie', 'make_panel_ref', 'panel_ref_matches']


class TokenReused(BadSignature):
    """The link was already opened once."""


def _actor_ids(actor: dict) -> dict:
    return {'zoho_user_id': str((actor or {}).get('zoho_user_id') or '')[:32]}


def make_panel_token(source: dict, actor: dict) -> str:
    payload = {'m': str(source['module']), 'r': str(source['id']), 'a': _actor_ids(actor), 'act': ACTION,
               'jti': uuid.uuid4().hex}
    return TimestampSigner(salt=TOKEN_SALT).sign_object(payload)


def make_panel_url(source: dict, actor: dict) -> str:
    """`https://<BASE_URL host>/crm/panel/?t=<token>` for one source record (`{"module", "id"}`)."""
    base = str(getattr(settings, 'BASE_URL', '') or '').rstrip('/')
    return f'{base}{reverse("crm_panel_open")}?t={make_panel_token(source, actor)}'


def read_panel_token(token: str, *, max_age: int = TOKEN_MAX_AGE_S) -> dict:
    """The token payload; raises BadSignature, SignatureExpired or TokenReused (the jti is claimed here)."""
    payload = TimestampSigner(salt=TOKEN_SALT).unsign_object(token or '', max_age=max_age)
    if not isinstance(payload, dict) or payload.get('act') != ACTION or not payload.get('jti') \
            or not payload.get('m') or not payload.get('r'):
        raise BadSignature('not a panel token')
    if not cache.add(JTI_KEY.format(jti=payload['jti']), 1, max_age):
        raise TokenReused('panel link already used')
    return payload


def read_panel_cookie(request) -> dict | None:
    """`{m, r, a, sid, initials}` of an open panel, or None (missing, tampered or older than 1800 s)."""
    try:
        state = signing.loads(request.COOKIES.get(COOKIE_NAME, ''), salt=COOKIE_SALT, max_age=COOKIE_MAX_AGE_S)
    except BadSignature:
        return None
    return state if isinstance(state, dict) and state.get('m') and state.get('r') else None


def _ref_fields(state: dict) -> dict:
    return {'m': str(state.get('m') or ''), 'r': str(state.get('r') or ''), 'sid': str(state.get('sid') or '')}


def make_panel_ref(state: dict) -> str:
    """The record a token-panel page was rendered for, posted back with each of its forms."""
    return signing.dumps(_ref_fields(state), salt=FORM_SALT)


def panel_ref_matches(ref: str | None, state: dict) -> bool:
    """Whether a posted `panel_ref` names the cookie's panel (same record and the same opened link)."""
    try:
        posted = signing.loads(ref or '', salt=FORM_SALT, max_age=COOKIE_MAX_AGE_S)
    except BadSignature:
        return False
    return isinstance(posted, dict) and posted == _ref_fields(state)


def set_panel_cookie(response, state: dict) -> None:
    response.set_cookie(COOKIE_NAME, signing.dumps(state, salt=COOKIE_SALT), max_age=COOKIE_MAX_AGE_S,
                        path=COOKIE_PATH, secure=True, httponly=True, samesite='Strict')
