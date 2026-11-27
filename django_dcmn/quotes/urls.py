"""Routes under /api/quote-requests/ (final list; the views are replaced, not this file)."""

from django.urls import path

from .views import QuoteConfigView, QuoteSubmissionView, QuoteUploadView

urlpatterns = [
    path('', QuoteSubmissionView.as_view(), name='quote_requests_submit'),
    path('config/', QuoteConfigView.as_view(), name='quote_requests_config'),
    path('uploads/', QuoteUploadView.as_view(), name='quote_requests_uploads'),
]
