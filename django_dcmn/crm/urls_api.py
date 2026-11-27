"""Routes under /api/crm/v1/ (final list; the views are replaced, not this file)."""

from django.urls import path

from .api_views import JobView, PanelLinkView, RelatedServicesView

urlpatterns = [
    path('related-services/', RelatedServicesView.as_view(), name='crm_related_services'),
    path('jobs/<uuid:job_id>/', JobView.as_view(), name='crm_job'),
    path('panel-links/', PanelLinkView.as_view(), name='crm_panel_links'),
]
