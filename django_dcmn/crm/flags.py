"""Engine and Zoho flags (DESIGN §13.1).

Every function reads settings at call time, so `override_settings` works in tests and a value that is
not one of the allowed choices falls back to the safe default.
"""

from typing import Literal

from django.conf import settings

EngineMode = Literal['off', 'dry_run', 'live']
RuleMode = Literal['off', 'dry_run', 'suggest', 'create']
AdoptionMode = Literal['auto_exact', 'confirm_all', 'off']
CreatedEmailPolicy = Literal['never', 'first_per_quote']

RULE_MODE_ORDER: tuple[RuleMode, ...] = ('off', 'dry_run', 'suggest', 'create')


def read(name: str, default):
    """A setting by name (the helpers below are shared with quotes.flags)."""
    return getattr(settings, name, default)


def read_choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    value = str(read(name, default) or '').strip().lower()
    return value if value in allowed else default


def read_emails(name: str) -> list[str]:
    """A recipient list setting; empty falls back to EMAIL_OFFICE_RECEIVER."""
    emails = [e.strip() for e in (read(name, None) or []) if e and e.strip()]
    if emails:
        return emails
    return [e.strip() for e in (read('EMAIL_OFFICE_RECEIVER', None) or []) if e and e.strip()]


# --- Engine -------------------------------------------------------------------------------------

def engine_mode() -> EngineMode:
    return read_choice('CRM_ENGINE_MODE', ('off', 'dry_run', 'live'), 'off')  # type: ignore[return-value]


def engine_test_only() -> bool:
    return bool(read('CRM_ENGINE_TEST_ONLY', True))


def sources_allowed() -> list[str]:
    return list(read('CRM_SOURCES_ALLOWED', ['Get_A_Quote_Leads']) or [])


def action_keys_raw() -> str:
    """`caller:key,…` as configured; crm.auth parses it. Empty means every call is refused."""
    return str(read('CRM_ACTION_KEYS', '') or '')


def create_limit_per_source_hour() -> int:
    return int(read('CRM_CREATE_LIMIT_PER_SOURCE_HOUR', 10))


def create_limit_global_hour() -> int:
    return int(read('CRM_CREATE_LIMIT_GLOBAL_HOUR', 60))


def panel_link_alert_hour() -> int:
    return int(read('CRM_PANEL_LINK_ALERT_HOUR', 30))


def rules_max_mode() -> RuleMode:
    return read_choice('CRM_RULES_MAX_MODE', RULE_MODE_ORDER, 'suggest')  # type: ignore[return-value]


def intake_candidate_days() -> int:
    return int(read('INTAKE_CANDIDATE_DAYS', 30))


def engine_inline() -> bool:
    return bool(read('ENGINE_INLINE', True))


def tid_policy() -> dict[str, str]:
    return dict(read('ENGINE_TID_POLICY', {}) or {})


def tid_created_email() -> CreatedEmailPolicy:
    return read_choice('ENGINE_TID_CREATED_EMAIL', ('never', 'first_per_quote'), 'never')  # type: ignore[return-value]


def trigger_for(module: str) -> list[str]:
    """Workflow triggers to send for a module; `{"*": []}` (never) unless a module opts in."""
    policy = read('ENGINE_TRIGGER_POLICY', {}) or {}
    return list(policy.get(module, policy.get('*', [])) or [])


def parity_constants() -> bool:
    return bool(read('ENGINE_PARITY_CONSTANTS', True))


def adoption_mode() -> AdoptionMode:
    return read_choice('ENGINE_ADOPTION', ('auto_exact', 'confirm_all', 'off'), 'auto_exact')  # type: ignore[return-value]


def orphan_scan_enabled() -> bool:
    return bool(read('ENGINE_ORPHAN_SCAN', True))


def copy_files() -> bool:
    return bool(read('ENGINE_COPY_FILES', False))


def apostille_fbi_stage() -> str:
    return str(read('APOSTILLE_FBI_STAGE', '') or '')


def translation_capture_enabled() -> bool:
    return bool(read('TRANSLATION_CAPTURE_ENABLED', False))


def translation_capture_types() -> list[str]:
    return list(read('TRANSLATION_CAPTURE_TYPES', []) or [])


# --- Zoho ---------------------------------------------------------------------------------------

def owner_for(service_key: str) -> str | None:
    """Zoho user id that owns new records of a service; None = the API user ("Customer Service")."""
    owner = (read('ZOHO_OWNER_BY_SERVICE', {}) or {}).get(service_key)
    return str(owner) if owner else None


def org_id() -> str:
    return str(read('ZOHO_ORG_ID', 'org882204370') or 'org882204370')


def schema_apply_allowed() -> bool:
    return bool(read('ZOHO_SCHEMA_APPLY_ALLOWED', False))


def schema_log_dir() -> str:
    return str(read('ZOHO_SCHEMA_LOG_DIR', 'var'))


def zoho_background_enabled() -> bool:
    """Schema refresh, registry validation and token pre-warm run only while a feature is on (S0 gate)."""
    return engine_mode() != 'off' or bool(read('QUOTE_V3_ENABLED', False))


# --- Ops and e2e --------------------------------------------------------------------------------

def ops_alert_emails() -> list[str]:
    return read_emails('OPS_ALERT_EMAILS')


def e2e_allowed() -> bool:
    return bool(read('E2E_ALLOWED', False))


def e2e_attribution() -> bool:
    return bool(read('E2E_ATTRIBUTION', False))


def e2e_email_base() -> str:
    return str(read('E2E_EMAIL_BASE', '') or '')
