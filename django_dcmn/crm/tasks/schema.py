"""Zoho metadata cache refresh (DESIGN §7.10) and registry validation (§7.4)."""

import logging

from celery import shared_task

from orders.zoho_errors import ZohoPermanentError, ZohoTransientError

from .. import flags, schema
from ..alerts import alert_ops

logger = logging.getLogger(__name__)


@shared_task(name='crm.tasks.refresh_zoho_schema')
def refresh_zoho_schema() -> str:
    """Beat, every 6 h and on worker_ready; only while crm.flags.zoho_background_enabled().

    A module Zoho cannot serve keeps its cached metadata or last snapshot. Registry problems alert `schema_drift`.
    """
    if not flags.zoho_background_enabled():
        return 'skipped'
    changed = failed = 0
    for module in schema.REFRESH_MODULES:
        try:
            changed += schema.refresh(module)
        except (ZohoTransientError, ZohoPermanentError) as exc:
            failed += 1
            logger.warning('zoho_schema_refresh_failed module=%s code=%s', module, getattr(exc, 'code', ''))
    problems = schema.validate_registry()
    if problems:
        logger.warning('zoho_schema_registry_problems count=%s', len(problems))
        alert_ops('schema_drift', 'registry', 'Zoho schema drift: registry validation failed',
                  'crm.registry.validate found:\n' + '\n'.join(f'- {p}' for p in problems))
    return f'changed={changed} failed={failed} problems={len(problems)}'
