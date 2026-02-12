# orders/zoho_client.py
"""
Zoho CRM Client wrapper for WhatConverts integration.
Provides a unified interface for creating records and attribution records.
"""

import requests
import logging
from django.conf import settings
from .zoho_sync import get_access_token, ZOHO_API_DOMAIN, ZOHO_ATTRIBUTION_MODULE

logger = logging.getLogger(__name__)


class ZohoCRMClient:
    """
    Simple wrapper around Zoho CRM API for WhatConverts phone lead sync.
    """

    def __init__(self):
        self.api_domain = ZOHO_API_DOMAIN
        self.access_token = None

    def _get_headers(self):
        """Get authorization headers for Zoho API."""
        if not self.access_token:
            self.access_token = get_access_token()

        return {
            "Authorization": f"Zoho-oauthtoken {self.access_token}",
            "Content-Type": "application/json"
        }

    def create_record(self, module_name, data):
        """
        Create a record in Zoho CRM.

        Args:
            module_name: Zoho module name (FBI_Apostille, Marriage_Orders, etc.)
            data: Record data dictionary

        Returns:
            API response dict or None on error
        """
        url = f"{self.api_domain}/crm/v2/{module_name}"
        headers = self._get_headers()
        payload = {"data": [data]}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create record in {module_name}: {e}")
            return None

    def create_attribution_record(self, attribution_data):
        """
        Create a Lead Attribution Record in Zoho.

        Args:
            attribution_data: Attribution record data

        Returns:
            Attribution record ID or None on error
        """
        url = f"{self.api_domain}/crm/v2/{ZOHO_ATTRIBUTION_MODULE}"
        headers = self._get_headers()
        payload = {"data": [attribution_data]}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            result = response.json()

            if result and result.get('data'):
                return result['data'][0]['details']['id']
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create attribution record: {e}")
            return None
