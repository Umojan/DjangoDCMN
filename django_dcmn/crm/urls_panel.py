"""Routes under /crm/panel/ (final list; the views are replaced, not this file)."""

from django.urls import path

from .panel.views import panel_act, panel_open, panel_zoho

urlpatterns = [
    path('', panel_open, name='crm_panel_open'),
    path('act/', panel_act, name='crm_panel_act'),
    path('zoho/', panel_zoho, name='crm_panel_zoho'),
]
