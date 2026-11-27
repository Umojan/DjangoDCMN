"""
URL configuration for django_dcmn project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.conf.urls.static import static
from django.urls import path, include
from crm.translation_capture import (
    ApostilleOrderWithTranslation,
    EmbassyOrderWithTranslation,
    FbiOrderWithTranslation,
    FingerprintingWithTranslation,
    MarriageOrderWithTranslation,
    PreCheckWithTranslation,
)
from .views import serve_media_file

urlpatterns = [
    path('admin/', admin.site.urls),
    # Get a Quote v3 intake and the CRM engine (flag-gated; must stay above the orders include)
    path('api/quote-requests/', include('quotes.urls')),
    path('api/crm/v1/', include('crm.urls_api')),
    path('crm/panel/', include('crm.urls_panel')),
    # Translation checkbox capture (TRANSLATION_CAPTURE_ENABLED; a pure pass-through when off): the legacy form
    # paths and names of orders/urls.py, routed to thin subclasses of the legacy views. Must stay above the
    # orders include, which keeps the same routes (now unreachable for these paths).
    path('api/fbi/create-order/', FbiOrderWithTranslation.as_view(), name='fbi_create_order'),
    path('api/marriage/create-order/', MarriageOrderWithTranslation.as_view(), name='marriage_create_order'),
    path('api/embassy/create-order/', EmbassyOrderWithTranslation.as_view(), name='create-embassy-order'),
    path('api/apostille/create-order/', ApostilleOrderWithTranslation.as_view(), name='create-apostille-order'),
    path('api/pre-check/submit/', PreCheckWithTranslation.as_view(), name='pre-check-submit'),
    path('api/fingerprinting/submit/', FingerprintingWithTranslation.as_view(), name='fingerprinting-submit'),
    path('api/', include('orders.urls')),
    path('api/reviews/', include('reviews.urls')),

    path('media/<path:path>', serve_media_file),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
