"""
Quick script to check Zoho CRM picklist values for stage/status fields.
Run: python3 manage.py shell < check_zoho_picklists.py
"""
import requests
from django_dcmn.orders.zoho_sync import get_access_token, ZOHO_API_DOMAIN

token = get_access_token()
headers = {"Authorization": f"Zoho-oauthtoken {token}"}

MODULES_TO_CHECK = {
    'Deals': 'Status',
    'Embassy_Legalization': 'Status',
    'Translation_Services': 'Translation_Status',
    'Apostille_Services': 'Status',
    'Triple_Seal_Apostilles': 'Stage',
    'I_9_Verification': 'Stage',
    'Get_A_Quote_Leads': 'GET_A_QUOTE_LEADS',
}

for module, field in MODULES_TO_CHECK.items():
    url = f"{ZOHO_API_DOMAIN}/crm/v2/settings/fields?module={module}"
    resp = requests.get(url, headers=headers)

    if resp.status_code != 200:
        print(f"\n❌ {module}: HTTP {resp.status_code}")
        continue

    data = resp.json()
    fields = data.get('fields', [])

    target_field = None
    for f in fields:
        if f.get('api_name') == field:
            target_field = f
            break

    if not target_field:
        print(f"\n❌ {module}.{field}: Field NOT FOUND")
        continue

    pick_list = target_field.get('pick_list_values', [])
    values = [v.get('display_value') for v in pick_list]

    has_phone_call = 'Phone Call Received' in values
    print(f"\n{'✅' if has_phone_call else '❌'} {module}.{field}:")
    print(f"   Has 'Phone Call Received': {has_phone_call}")
    print(f"   All values: {values}")
