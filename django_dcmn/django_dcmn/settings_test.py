"""Settings for running the test suite locally without Redis / the production DB.

    DJANGO_SETTINGS_MODULE=django_dcmn.settings_test python3 manage.py test orders

Race tests (lease UPDATE, select_for_update, IntegrityError) need Postgres; they are tagged "postgres":

    TEST_POSTGRES_URL=postgres://… DJANGO_SETTINGS_MODULE=django_dcmn.settings_test python3 manage.py test quotes crm --tag=postgres
"""
import os

import dj_database_url

from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
if os.environ.get('TEST_POSTGRES_URL'):
    DATABASES = {'default': dj_database_url.parse(os.environ['TEST_POSTGRES_URL'])}
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
PII_HASH_SALT = 'test-only-pii-hash-salt-0123456789abcdef'   # QUOTE_V3_ENABLED refuses a missing salt
