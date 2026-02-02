import secrets
import string
from django.conf import settings
from .constants import SERVICE_LABELS


def check_zoho_webhook_token(request) -> bool:
    """
    Validate token from Zoho webhook.
    
    Checks:
    1. X-ZOHO-TOKEN header
    2. HTTP_X_ZOHO_TOKEN meta
    3. 'token' field in JSON body (fallback)
    
    Returns:
        True if token is valid, False otherwise
    """
    token = request.headers.get('X-ZOHO-TOKEN') or request.META.get('HTTP_X_ZOHO_TOKEN')
    # fallback: allow token in JSON body for setups without headers
    if not token:
        try:
            token = request.data.get('token')
        except Exception:
            token = None
    
    expected = getattr(settings, 'ZOHO_WEBHOOK_TOKEN', '')
    return bool(token and expected and token == expected)


def generate_tid(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def mask_email(email: str) -> str:
    if not email or '@' not in email:
        return ''
    name, domain = email.split('@', 1)
    return (name[:4] + '***@' + domain)


def public_name(full_name: str) -> str:
    if not full_name:
        return ''
    parts = full_name.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0]


def service_label(service: str) -> str:
    return SERVICE_LABELS.get(service, service)


