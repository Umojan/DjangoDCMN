class ZohoSyncError(Exception):
    """Base exception for failures while synchronizing with Zoho CRM."""


class ZohoTransientError(ZohoSyncError):
    """A temporary failure that Celery should retry."""


class ZohoPermanentError(ZohoSyncError):
    """A validation or configuration failure that needs human attention."""
