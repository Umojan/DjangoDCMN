from django.urls import path
from .views import CreateFbiOrderView

urlpatterns = [
    path('create-fbi-order/', CreateFbiOrderView.as_view(), name='create_fbi_order'),
]