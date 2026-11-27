"""Intake flags (DESIGN §13.1), read at call time so `override_settings` works."""

import logging
from typing import Literal

from crm import flags as crm_flags
from crm.flags import read, read_choice, read_emails

logger = logging.getLogger(__name__)

IntakeMode = Literal['none', 'primary']
SubformMode = Literal['off', 'auto']
PII_HASH_SALT_MIN = 32


def quote_v3_enabled() -> bool:
    """QUOTE_V3_ENABLED, refused (the endpoint answers 503) while PII_HASH_SALT is missing or short: rejection
    rows would store reversible IP and email hashes (DESIGN §13.1)."""
    if not bool(read('QUOTE_V3_ENABLED', False)):
        return False
    if len(pii_hash_salt()) < PII_HASH_SALT_MIN:
        logger.error('quote_v3_refused_pii_hash_salt_unset min_len=%s', PII_HASH_SALT_MIN)
        try:
            from crm.alerts import alert_ops

            alert_ops('pii_hash_salt', 'unset', 'Quote v3 is off: PII_HASH_SALT is not set',
                      f'QUOTE_V3_ENABLED is on but PII_HASH_SALT is shorter than {PII_HASH_SALT_MIN} characters, so '
                      'the v3 quote endpoint answers 503 and the page shows the legacy form. Set PII_HASH_SALT '
                      '(python3 -c "import secrets; print(secrets.token_hex(32))") on Railway and redeploy.')
        except Exception:
            logger.exception('quote_v3_salt_alert_failed')
        return False
    return True


def v3_test_only() -> bool:
    return bool(read('QUOTE_V3_TEST_ONLY', True))


def zoho_sync() -> bool:
    return bool(read('QUOTE_V3_ZOHO_SYNC', True))


def auto_intake() -> IntakeMode:
    return read_choice('QUOTE_AUTO_INTAKE', ('none', 'primary'), 'none')  # type: ignore[return-value]


def intake_mode_snapshot() -> IntakeMode:
    """`intake_mode` stored on a new quote: `primary` only with auto-intake on and the engine live (DESIGN §7.3)."""
    if auto_intake() == 'primary' and crm_flags.engine_mode() == 'live':
        return 'primary'
    return 'none'


def subform_mode() -> SubformMode:
    return read_choice('QUOTE_V3_SUBFORM', ('off', 'auto'), 'off')  # type: ignore[return-value]


def confirmation_email() -> bool:
    return bool(read('QUOTE_V3_CONFIRMATION_EMAIL', False))


def v3_test_emails() -> bool:
    return bool(read('QUOTE_V3_TEST_EMAILS', False))


def reply_to() -> str:
    return str(read('QUOTE_REPLY_TO', '') or 'support@dcmobilenotary.com')


def uploads_enabled() -> bool:
    return bool(read('QUOTE_V3_UPLOADS_ENABLED', False))


def turnstile_required() -> bool:
    return bool(read('QUOTE_TURNSTILE_REQUIRED', False))


def hourly_ceiling() -> int:
    return int(read('QUOTES_HOURLY_CEILING', 60))


def proxy_hops() -> int:
    return max(int(read('QUOTES_PROXY_HOPS', 1)), 0)


def shadow_legacy() -> bool:
    return bool(read('QUOTE_V3_SHADOW_LEGACY', False))


def notify_emails() -> list[str]:
    return read_emails('QUOTES_NOTIFY_EMAILS')


def test_notify_emails() -> list[str]:
    return read_emails('QUOTES_TEST_NOTIFY_EMAILS')


def pii_hash_salt() -> str:
    return str(read('PII_HASH_SALT', '') or '')


def quote_owner_id() -> str | None:
    """Zoho user id for new GAQL records; None = the API user ("Customer Service")."""
    return str(read('ZOHO_QUOTE_OWNER_ID', '') or '') or None
