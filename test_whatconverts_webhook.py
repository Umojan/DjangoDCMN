#!/usr/bin/env python3
"""
Test script for WhatConverts webhook integration.
Sends sample webhook data to test endpoint.
"""

import requests
import json

# Test data matching WhatConverts format
TEST_PAYLOADS = {
    'fbi_apostille': {
        "trigger": "new",
        "lead_id": 999001,
        "user_id": "test-user-001",
        "lead_type": "Phone Call",
        "lead_status": "Unique",
        "lead_analysis": {
            "Keyword Detection": "fbi,apostille,background check",
            "Lead Summary": "Customer needs FBI apostille for job abroad. Asked about turnaround time and pricing.",
            "Intent Detection": "Ready to purchase, needs urgent processing",
            "Sentiment Detection": "Positive",
            "Topic Detection": "fbi apostille,international employment,document authentication"
        },
        "last_updated": "2026-02-04T15:30:00Z",
        "date_created": "2026-02-04T15:30:00Z",
        "lead_score": 75,
        "contact_name": "John Smith",
        "contact_email_address": "john.smith@example.com",
        "contact_phone_number": "+1-555-123-4567",
        "email_address": "john.smith@example.com",
        "phone_number": "+1-555-123-4567",
        "landing_url": "https://dcmn.us/apostille-fbi",
        "lead_url": "https://dcmn.us/apostille-fbi-form",
        "lead_source": "google",
        "lead_medium": "cpc",
        "lead_campaign": "fbi apostille services",
        "lead_keyword": "fbi apostille near me",
        "city": "Los Angeles",
        "state": "CA",
        "zip": "90001",
        "country": "US",
        "operating_system": "iOS 16",
        "browser": "Safari Mobile",
        "device_type": "Smartphone",
        "device_make": "Apple iPhone 14",
        "gclid": "test_gclid_12345",
        "duplicate": False,
        "spam": False
    },

    'marriage': {
        "trigger": "new",
        "lead_id": 999002,
        "lead_type": "Phone Call",
        "lead_status": "Unique",
        "lead_analysis": {
            "Lead Summary": "Needs triple seal marriage certificate for immigration.",
            "Sentiment Detection": "Neutral",
            "Intent Detection": "Gathering information"
        },
        "date_created": "2026-02-04T16:00:00Z",
        "lead_score": 60,
        "contact_name": "Maria Garcia",
        "contact_email_address": "maria.g@example.com",
        "phone_number": "+1-555-987-6543",
        "landing_url": "https://dcmn.us/triple-seal-marriage",
        "lead_source": "yelp",
        "lead_medium": "organic",
        "city": "New York",
        "state": "NY",
        "country": "US",
        "device_type": "Desktop",
        "spam": False
    },

    'translation': {
        "trigger": "new",
        "lead_id": 999003,
        "lead_type": "Phone Call",
        "lead_status": "Unique",
        "lead_analysis": {
            "Lead Summary": "Needs Spanish to English translation for legal documents.",
            "Sentiment Detection": "Positive"
        },
        "date_created": "2026-02-04T17:00:00Z",
        "lead_score": 85,
        "contact_name": "Robert Johnson",
        "phone_number": "+1-555-456-7890",
        "landing_url": "https://dcmn.us/translation-services",
        "lead_source": "google",
        "lead_medium": "cpc",
        "campaign": "translation services 2026",
        "city": "Miami",
        "state": "FL",
        "country": "US",
        "spam": False
    },

    'unknown_service': {
        "trigger": "new",
        "lead_id": 999004,
        "lead_type": "Phone Call",
        "lead_status": "Unique",
        "date_created": "2026-02-04T18:00:00Z",
        "lead_score": 40,
        "contact_name": "Sarah Wilson",
        "phone_number": "+1-555-111-2222",
        "landing_url": "https://dcmn.us/",  # Homepage - no specific service
        "lead_source": "direct",
        "city": "Chicago",
        "state": "IL",
        "country": "US",
        "spam": False
    },

    'tracking_page': {
        "trigger": "new",
        "lead_id": 999005,
        "lead_type": "Phone Call",
        "date_created": "2026-02-04T19:00:00Z",
        "contact_name": "Test User",
        "phone_number": "+1-555-999-0000",
        "landing_url": "https://dcmn.us/tracking/ABC123",  # Should be ignored
        "lead_source": "direct",
        "spam": False
    },

    'web_form_not_phone': {
        "trigger": "new",
        "lead_id": 999006,
        "lead_type": "Web Form",  # Should be ignored (not Phone Call)
        "date_created": "2026-02-04T20:00:00Z",
        "contact_name": "Form Submitter",
        "email_address": "form@example.com",
        "landing_url": "https://dcmn.us/apostille-fbi",
        "spam": False
    },
}


def test_webhook(endpoint_url: str, test_name: str, payload: dict):
    """Send test payload to webhook endpoint."""

    print("=" * 80)
    print(f"🧪 Testing: {test_name}")
    print("=" * 80)
    print(f"📤 Sending to: {endpoint_url}")
    print(f"📋 Payload lead_id: {payload.get('lead_id')}")
    print(f"📋 Lead type: {payload.get('lead_type')}")
    print(f"📋 Landing URL: {payload.get('landing_url')}")
    print()

    try:
        response = requests.post(
            endpoint_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )

        print(f"✅ Status Code: {response.status_code}")
        print(f"📥 Response:")
        print(json.dumps(response.json(), indent=2))
        print()

        return response.status_code == 200

    except requests.exceptions.RequestException as e:
        print(f"❌ Error: {e}")
        print()
        return False


def main():
    """Run all tests."""

    # Configuration
    BASE_URL = "http://localhost:8000"  # Change to your domain
    TEST_ENDPOINT = f"{BASE_URL}/api/orders/webhook/whatconverts-test/"
    PROD_ENDPOINT = f"{BASE_URL}/api/orders/webhook/whatconverts/"

    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║                   WhatConverts Webhook Integration Test                      ║
╚══════════════════════════════════════════════════════════════════════════════╝
    """)

    # Ask which endpoint to test
    print("Which endpoint do you want to test?")
    print("1. Test endpoint (logs only)")
    print("2. Production endpoint (full processing)")
    choice = input("\nEnter choice (1 or 2): ").strip()

    if choice == "1":
        endpoint = TEST_ENDPOINT
        print(f"\n✅ Using TEST endpoint: {endpoint}\n")
    elif choice == "2":
        endpoint = PROD_ENDPOINT
        print(f"\n✅ Using PRODUCTION endpoint: {endpoint}\n")
    else:
        print("❌ Invalid choice")
        return

    # Ask which tests to run
    print("\nAvailable tests:")
    for idx, name in enumerate(TEST_PAYLOADS.keys(), 1):
        print(f"{idx}. {name}")
    print(f"{len(TEST_PAYLOADS) + 1}. Run all tests")

    test_choice = input("\nEnter test number (or 'all'): ").strip()

    results = {}

    if test_choice.lower() == 'all' or test_choice == str(len(TEST_PAYLOADS) + 1):
        # Run all tests
        for test_name, payload in TEST_PAYLOADS.items():
            results[test_name] = test_webhook(endpoint, test_name, payload)
    else:
        # Run specific test
        try:
            test_idx = int(test_choice) - 1
            test_name = list(TEST_PAYLOADS.keys())[test_idx]
            payload = TEST_PAYLOADS[test_name]
            results[test_name] = test_webhook(endpoint, test_name, payload)
        except (ValueError, IndexError):
            print("❌ Invalid test number")
            return

    # Summary
    print("=" * 80)
    print("📊 TEST SUMMARY")
    print("=" * 80)
    for test_name, success in results.items():
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status}: {test_name}")
    print()

    total = len(results)
    passed = sum(results.values())
    print(f"Total: {passed}/{total} tests passed")
    print()


if __name__ == "__main__":
    main()
