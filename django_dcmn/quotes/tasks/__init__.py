"""Celery tasks of the quotes app.

Each submodule belongs to one work package and the names are final (IMPLEMENTATION-PLAN WP1), so this file
does not change when a work package replaces its submodule.
"""

from .emails import send_quote_confirmation_email, send_quote_staff_email
from .maintenance import link_recent_phone_leads, purge_quote_pii
from .pipeline import process_quote_submission, reconcile_quote_submissions, shadow_map_legacy_quote

__all__ = [
    'process_quote_submission',
    'reconcile_quote_submissions',
    'shadow_map_legacy_quote',
    'send_quote_staff_email',
    'send_quote_confirmation_email',
    'link_recent_phone_leads',
    'purge_quote_pii',
]
