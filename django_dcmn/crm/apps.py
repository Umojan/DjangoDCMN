from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crm'
    verbose_name = 'CRM engine'

    def ready(self):
        from celery.signals import worker_ready

        worker_ready.connect(refresh_schema_on_worker_ready, weak=False, dispatch_uid='crm.refresh_zoho_schema')


def refresh_schema_on_worker_ready(sender=None, **kwargs):
    """Refresh the Zoho metadata cache when a worker starts, only while a feature is on (S0: no new traffic)."""
    from . import flags
    from .tasks import refresh_zoho_schema

    if flags.zoho_background_enabled():
        refresh_zoho_schema.delay()
