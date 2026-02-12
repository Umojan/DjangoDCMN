# orders/views/__init__.py
"""
Views package for orders app.
Re-exports all views for backwards compatibility with urls.py
"""

from .orders import (
    CreateFbiOrderView,
    CreateMarriageOrderView,
    CreateEmbassyOrderView,
    CreateApostilleOrderView,
    CreateTranslationOrderView,
    CreateQuoteRequestView,
    CreateI9OrderView,
    FbiOptionsView,
)

from .stripe import (
    CreateStripeSessionView,
    stripe_webhook,
)

from .tracking import (
    CreateTidFromCrmView,
    CrmUpdateStageView,
    PublicTrackView,
)

from .webhooks import (
    whatconverts_test_webhook,
    whatconverts_webhook,
)

from .misc import (
    test_email,
    zoho_callback,
)

__all__ = [
    # Order creation views
    'CreateFbiOrderView',
    'CreateMarriageOrderView',
    'CreateEmbassyOrderView',
    'CreateApostilleOrderView',
    'CreateTranslationOrderView',
    'CreateQuoteRequestView',
    'CreateI9OrderView',
    'FbiOptionsView',
    # Stripe
    'CreateStripeSessionView',
    'stripe_webhook',
    # Tracking
    'CreateTidFromCrmView',
    'CrmUpdateStageView',
    'PublicTrackView',
    # Webhooks
    'whatconverts_test_webhook',
    'whatconverts_webhook',
    # Misc
    'test_email',
    'zoho_callback',
]
