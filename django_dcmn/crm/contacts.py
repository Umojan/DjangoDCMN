"""One shared Zoho Contact find-or-create (DESIGN §7.2), used by quotes step 1 and by the engine."""

import logging
import re

from orders.zoho_sync import _split_contact_name

from . import zoho

logger = logging.getLogger(__name__)

CONTACTS_MODULE = 'Contacts'
ZOHO_EMAIL_MAX = 100  # Zoho email fields hold at most 100 characters
ZOHO_PHONE_MAX = 30
DEFAULT_LAST_NAME = 'DCMN Client'
DIFFERENCE_FIELDS = ('Full_Name', 'First_Name', 'Last_Name', 'Phone', 'Mobile')


def find_or_create(name: str, email: str | None, phone: str | None) -> tuple[str | None, str]:
    """Return (contact id, action) with action `matched`, `created` or `skipped`.

    Search by email; an existing contact is only filled in (Phone, First_Name), never overwritten. No email,
    or one longer than Zoho accepts, skips the contact. Creates and updates send `trigger: []`.
    """
    email = (email or '').strip()
    if not email or len(email) > ZOHO_EMAIL_MAX:
        return None, 'skipped'
    phone = (phone or '').strip()[:ZOHO_PHONE_MAX]

    contact = _find_by_email(email)
    if contact:
        _fill_blanks(contact, name, phone)
        return str(contact['id']), 'matched'

    first_name, last_name = _name_parts(name)
    record = {'Last_Name': last_name, 'Email': email}
    if first_name:
        record['First_Name'] = first_name
    if phone:
        record['Phone'] = phone
    try:
        body = zoho.zoho_call('POST', CONTACTS_MODULE, json={'data': [record], 'trigger': []})
        return zoho.result_id(body, 'POST', CONTACTS_MODULE), 'created'
    except zoho.ZohoPermanent as exc:
        if exc.code != 'DUPLICATE_DATA':
            raise
        duplicate_id = exc.duplicate_id
    # A parallel create won the race on the unique Email.
    if duplicate_id:
        return duplicate_id, 'matched'
    contact = _find_by_email(email)
    if not contact:
        raise zoho.ZohoTransient('POST Contacts: DUPLICATE_DATA, contact not searchable yet', code='DUPLICATE_DATA')
    return str(contact['id']), 'matched'


def differences(contact_id: str, name: str, phone_last10: str) -> list[str]:
    """`Contact on file has another phone: …` / `… another name: …` for the receipt Note (DESIGN §5.2 step 1).

    One GET of the matched Contact after its blanks were filled; a Zoho error leaves the lines out.
    """
    try:
        contact = zoho.get(CONTACTS_MODULE, contact_id, fields=list(DIFFERENCE_FIELDS))
    except (zoho.ZohoTransient, zoho.ZohoPermanent) as exc:
        logger.warning('zoho_contact_read_failed id=%s code=%s', contact_id, exc.code)
        return []
    if not contact:
        return []
    rows = []
    phones = [str(contact[f]) for f in ('Phone', 'Mobile') if contact.get(f)]
    if phone_last10 and phones and phone_last10 not in {re.sub(r'\D', '', p)[-10:] for p in phones}:
        rows.append(f'Contact on file has another phone: {", ".join(phones)}')
    on_file = contact.get('Full_Name') or ' '.join(str(contact[f]) for f in ('First_Name', 'Last_Name')
                                                    if contact.get(f))
    if on_file and on_file != DEFAULT_LAST_NAME and _folded(on_file) != _folded(name):
        rows.append(f'Contact on file has another name: {on_file}')
    return rows


def _folded(name: str) -> str:
    return ' '.join(str(name or '').casefold().split())


def _find_by_email(email: str) -> dict | None:
    body = zoho.zoho_call('GET', f'{CONTACTS_MODULE}/search', params={'email': email})
    records = (body or {}).get('data') or []
    return records[0] if records else None


def _fill_blanks(contact: dict, name: str, phone: str) -> None:
    fields = {}
    if phone and not contact.get('Phone'):
        fields['Phone'] = phone
    first_name, _ = _name_parts(name)
    if first_name and not contact.get('First_Name'):
        fields['First_Name'] = first_name
    if fields:
        zoho.update(CONTACTS_MODULE, str(contact['id']), fields)
        logger.info('zoho_contact_filled id=%s fields=%s', contact['id'], ','.join(sorted(fields)))


def _name_parts(name: str) -> tuple[str, str]:
    """(First_Name ≤40, Last_Name ≤80); a first name only when the name has two or more words."""
    if not (name or '').strip():
        return '', DEFAULT_LAST_NAME
    return _split_contact_name(name)
