# orders/urls.py
from django.urls import path
from .views import (
    CreateFbiOrderView,
    FbiOptionsView,
    CreateStripeSessionView,
    stripe_webhook,
    test_email, CreateMarriageOrderView,
)

urlpatterns = [
    path('fbi/create-order/', CreateFbiOrderView.as_view(), name='fbi_create_order'),
    path('fbi/options/', FbiOptionsView.as_view(), name='fbi_options'),
    path("fbi/create-stripe-session/", CreateStripeSessionView.as_view(), name="create_stripe_session"),

    path('marriage/create-order/', CreateMarriageOrderView.as_view(), name='marriage_create_order'),


    path("webhook/stripe/", stripe_webhook),

    path("test-email/", test_email, name="test_email"),
]
