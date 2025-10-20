import secrets
import string
from .constants import SERVICE_LABELS


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


