from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def pii_hash_salt_check(app_configs=None, **kwargs):
    """`check --deploy`: QUOTE_V3_ENABLED needs PII_HASH_SALT (the endpoint also refuses at run time)."""
    from .flags import PII_HASH_SALT_MIN

    if getattr(settings, 'QUOTE_V3_ENABLED', False) and len(getattr(settings, 'PII_HASH_SALT', '') or '') < \
            PII_HASH_SALT_MIN:
        return [Error(f'PII_HASH_SALT must be set (at least {PII_HASH_SALT_MIN} characters) when QUOTE_V3_ENABLED.',
                      hint='python3 -c "import secrets; print(secrets.token_hex(32))"', id='quotes.E001')]
    return []


class QuotesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'quotes'
    verbose_name = 'Quotes (v3 intake)'

    def ready(self):
        # S1 shadow mode: the receiver on orders.QuoteRequest exists only while the flag is on (DESIGN §13.2).
        if getattr(settings, 'QUOTE_V3_SHADOW_LEGACY', False):
            from . import signals  # noqa: F401
