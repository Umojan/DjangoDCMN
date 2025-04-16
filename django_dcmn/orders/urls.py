# orders/urls.py
from django.urls import path
from .views import CreateFbiOrderView, FbiOptionsView

urlpatterns = [
    path('fbi/create-order/', CreateFbiOrderView.as_view(), name='fbi_create_order'),
    path('fbi/options/', FbiOptionsView.as_view(), name='fbi_options'),
]