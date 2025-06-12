# orders/urls.py
from django.urls import path
from .views import (
    CreateFbiOrderView,
    CreateMarriageOrderView,
    CreateEmbassyOrderView,

    FbiOptionsView,
    CreateStripeSessionView,

    stripe_webhook,
    test_email,

    zoho_callback,
)

urlpatterns = [
    path('fbi/create-order/', CreateFbiOrderView.as_view(), name='fbi_create_order'),
    path('marriage/create-order/', CreateMarriageOrderView.as_view(), name='marriage_create_order'),
    path('embassy/create-order/', CreateEmbassyOrderView.as_view(), name='create-embassy-order'),

    path('fbi/options/', FbiOptionsView.as_view(), name='fbi_options'),
    path("create-stripe-session/", CreateStripeSessionView.as_view(), name="create_stripe_session"),

    path("webhook/stripe/", stripe_webhook),

    path("test-email/", test_email, name="test_email"),

    path('zoho/callback/', zoho_callback, name='zoho_callback'),
]
