# orders/urls.py
from django.urls import path
from .views import (
    CreateFbiOrderView,
    CreateMarriageOrderView,
    CreateEmbassyOrderView,
    CreateTranslationOrderView,
    FbiOptionsView,
    CreateStripeSessionView,
    CreateApostilleOrderView,
    CreateI9OrderView,
    CreateQuoteRequestView,

    stripe_webhook,
    test_email,

    zoho_callback,
    CreateTidFromCrmView,
    CrmUpdateStageView,
    PublicTrackView,
    whatconverts_test_webhook,
    whatconverts_webhook,
)

urlpatterns = [
    path('fbi/create-order/', CreateFbiOrderView.as_view(), name='fbi_create_order'),
    path('marriage/create-order/', CreateMarriageOrderView.as_view(), name='marriage_create_order'),
    path('embassy/create-order/', CreateEmbassyOrderView.as_view(), name='create-embassy-order'),
    path('translation/create-order/', CreateTranslationOrderView.as_view(), name='create-translation-order'),
    path('apostille/create-order/', CreateApostilleOrderView.as_view(), name='create-apostille-order'),
    path('i9/create-order/', CreateI9OrderView.as_view(), name='create-i9-order'),
    path('quote/create-order/', CreateQuoteRequestView.as_view(), name='create-quote-request'),

    path('fbi/options/', FbiOptionsView.as_view(), name='fbi_options'),
    path("create-stripe-session/", CreateStripeSessionView.as_view(), name="create_stripe_session"),

    path("webhook/stripe/", stripe_webhook),
    path("webhook/whatconverts-test/", whatconverts_test_webhook, name="whatconverts_test"),
    path("webhook/whatconverts/", whatconverts_webhook, name="whatconverts"),

    path("test-email/", test_email, name="test_email"),

    path('zoho/callback/', zoho_callback, name='zoho_callback'),

    # Tracking
    path('tracking/crm/create/', CreateTidFromCrmView.as_view(), name='tracking_crm_create'),
    path('tracking/crm/update/', CrmUpdateStageView.as_view(), name='tracking_crm_update'),
    path('tracking/<str:tid>/', PublicTrackView.as_view(), name='tracking_public'),
]
