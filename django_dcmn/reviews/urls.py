from django.urls import path
from .views import FeedbackView, RateView, ReviewContextView, ReviewWebhookView

urlpatterns = [
    path('webhook/', ReviewWebhookView.as_view(), name='review_webhook'),
    path('feedback/', FeedbackView.as_view(), name='review_feedback'),
    path('r/<str:token>/<int:stars>/', RateView.as_view(), name='review_rate'),
    path('r/<str:token>/', ReviewContextView.as_view(), name='review_context'),
]
