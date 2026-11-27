"""Rules turn signals into engine calls (DESIGN §7.11); every rule is capped by CRM_RULES_MAX_MODE."""

import logging

from django.db.models import F
from django.utils import timezone

from . import engine, flags, sources
from .alerts import alert_ops
from .models import AutomationRule, ServiceSignal

logger = logging.getLogger(__name__)

RULE_DEFAULTS = {
    'translation_from_form': 'suggest',
    'apostille_implied_from_quote': 'suggest',
    'quote_accepted_create_remaining': 'off',
    'lead_qualified_create': 'off',
    'invoice_paid_create_remaining': 'off',
}
SIGNAL_RULES = {
    'apostille_implied': 'apostille_implied_from_quote',
    'translation_requested': 'translation_from_form',
    'quote_status_changed': 'quote_accepted_create_remaining',
    'lead_qualified': 'lead_qualified_create',
    'invoice_paid': 'invoice_paid_create_remaining',
}
STAR_RULES = frozenset({'quote_accepted_create_remaining', 'invoice_paid_create_remaining'})
OUTCOMES = {'dry_run': 'dry_run', 'suggest': 'suggested', 'create': 'created'}
RULES_ACTOR = {'kind': 'rule'}
RETRY_CODES = ('disabled', 'zoho_unavailable')
RETRY_MAX_ATTEMPTS = 50


def _rank(mode: str) -> int:
    return flags.RULE_MODE_ORDER.index(mode) if mode in flags.RULE_MODE_ORDER else 0


def effective_mode(key: str, *, alert: bool = True) -> str:
    """min(row mode or RULE_DEFAULTS, CRM_RULES_MAX_MODE); `create` without a recorded approval is `suggest`."""
    row = AutomationRule.objects.filter(key=key).first()
    mode = row.mode if row else RULE_DEFAULTS.get(key, 'off')
    mode = min(mode, flags.rules_max_mode(), key=_rank)
    if mode == 'create' and not (row and row.approved_by and row.approved_at):
        if not alert:
            return 'suggest'
        logger.warning('rule_create_unapproved rule=%s', key)
        alert_ops('rule_create_unapproved', key, f'Rule {key} set to create without approval',
                  f'AutomationRule {key} has mode=create but no approved_by/approved_at. It runs as suggest '
                  'until the approval is recorded in the admin.')
        return 'suggest'
    return mode


def _engine_unavailable(result) -> bool:
    """The engine could not answer now (off, Zoho down, circuit open): not the signal's fault."""
    return result.http_status >= 500 or result.request.error_code in RETRY_CODES


def evaluate(signal: ServiceSignal) -> str:
    """Run the signal's rule through the engine; returns and stores the outcome.

    While the engine is off or Zoho is unavailable the signal stays open (`retry`), so the suggestion is made
    once the engine is back; after RETRY_MAX_ATTEMPTS it is evaluated as `ignored`.
    """
    rule_key = SIGNAL_RULES.get(signal.kind, '')
    mode = effective_mode(rule_key) if rule_key else 'off'
    outcome = 'ignored'
    if mode != 'off':
        source = sources.for_quote(signal.quote) if signal.source_kind == 'quote' and signal.quote_id \
            else (signal.source_module, signal.source_record_id)
        services = ['*'] if rule_key in STAR_RULES else [signal.service_key]
        result = engine.handle(source, services, mode=mode, trigger=f'rule:{rule_key}'[:40], caller='rules',
                               actor={**RULES_ACTOR, 'rule': rule_key})
        if _engine_unavailable(result) and signal.attempts + 1 < RETRY_MAX_ATTEMPTS:
            ServiceSignal.objects.filter(pk=signal.pk).update(
                attempts=F('attempts') + 1, rule_key=rule_key, last_error=result.request.error_code)
            signal.refresh_from_db(fields=['attempts', 'rule_key', 'last_error'])
            logger.info('rule_retry signal=%s rule=%s code=%s', signal.pk, rule_key, result.request.error_code)
            return 'retry'
        outcome = OUTCOMES[mode] if result.http_status < 400 else 'ignored'
        if result.http_status >= 400:
            signal.last_error = result.request.error_code
    signal.state = 'evaluated'
    signal.outcome = outcome
    signal.rule_key = rule_key
    signal.attempts += 1
    signal.evaluated_at = timezone.now()
    signal.save(update_fields=['state', 'outcome', 'rule_key', 'attempts', 'evaluated_at', 'last_error'])
    logger.info('rule_evaluated signal=%s rule=%s mode=%s outcome=%s', signal.pk, rule_key, mode, outcome)
    return outcome
