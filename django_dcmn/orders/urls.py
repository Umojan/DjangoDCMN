# orders/urls.py
from django.urls import path
from .views import CreateFbiOrderView, FbiOptionsView, CreateStripeSessionView

urlpatterns = [
    path('fbi/create-order/', CreateFbiOrderView.as_view(), name='fbi_create_order'),
    path('fbi/options/', FbiOptionsView.as_view(), name='fbi_options'),
    path("fbi/create-stripe-session/", CreateStripeSessionView.as_view(), name="create_stripe_session"),
]