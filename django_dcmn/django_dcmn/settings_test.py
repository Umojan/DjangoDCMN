"""Settings for running the test suite locally without Redis / the production DB.

    DJANGO_SETTINGS_MODULE=django_dcmn.settings_test python3 manage.py test orders
"""
from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
EMAIL_OFFICE_RECEIVER = ['office@example.com']
APPLICATIONS_NOTIFY_EMAILS = ['jules@example.com', 'daveys@example.com']
ZOHO_APPLICATIONS_OWNER_IDS = []
TURNSTILE_SECRET_KEY = ''
