from django.urls import path
from .views import ReviewWebhookView

urlpatterns = [
    path('webhook/', ReviewWebhookView.as_view(), name='review_webhook'),
]
