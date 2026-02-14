# DjangoDCMN Backend — Comprehensive Documentation

## Project Structure

```
django_dcmn/
├── orders/
│   ├── models.py                    # All order models + PhoneCallLead + Track
│   ├── serializers.py               # DRF serializers for all order types
│   ├── tasks.py                     # Celery tasks (Zoho sync, tracking emails)
│   ├── constants.py                 # STAGE_DEFS per service
│   ├── utils.py                     # generate_tid(), service_label()
│   ├── urls.py                      # URL patterns (/api/...)
│   ├── zoho_sync.py                 # LEGACY form→Zoho sync functions (DO NOT TOUCH)
│   ├── zoho_client.py               # New ZohoCRMClient wrapper class
│   ├── views/
│   │   ├── orders.py                # Order creation API views
│   │   ├── stripe.py                # Stripe payment + webhook
│   │   └── webhooks.py              # WhatConverts webhook handler
│   └── services/
│       ├── __init__.py              # Exports process_new_order, save_file_attachments
│       ├── order_processing.py      # process_new_order() pipeline
│       ├── attribution.py           # Attribution extraction, Zoho payload, phone lead check
│       ├── phone_lead_matcher.py    # Phone↔Form matching, conditional stage advancement
│       ├── whatconverts.py           # WhatConverts webhook parsing, service detection, dedup
│       ├── whatconverts_zoho.py      # Phone lead → Zoho CRM sync
│       ├── files.py                 # File attachment handling
│       ├── notifications.py         # Staff email notifications
│       └── tracking.py              # Order tracking (TID creation)
```

- URL prefix: `/api/` (NOT `/api/orders/`)
- Uses `python3` (not `python`)
- Celery for async tasks (Zoho sync, emails)
- Resend as email provider (not SMTP)

---

## Models (Key Facts)

- `EmbassyLegalizationOrder` (NOT `EmbassyOrder`) — common import mistake
- All order models have `zoho_synced` (BooleanField) but NOT `zoho_id`
- `zoho_synced = True` prevents Celery from creating duplicate Zoho records
- `PhoneCallLead` has: `zoho_lead_id`, `zoho_attribution_id`, `zoho_module`, `zoho_synced`
- `PhoneCallLead` has: `matched_with_form`, `matched_order_type`, `matched_order_id`
- `QuoteRequest` has `services` (TextField) — comma-separated selected services
- `Track` stores order tracking data (TID, stages, emails)

---

## Zoho CRM Modules

| Service    | Zoho Module              | Stage/Status Field       | "Form Received" Stage Value  |
|------------|--------------------------|--------------------------|------------------------------|
| fbi        | Deals                    | Stage                    | Order Received               |
| embassy    | Embassy_Legalization     | Status                   | Order Received               |
| translation| Translation_Services     | Translation_Status       | Client Placed Request        |
| apostille  | Apostille_Services       | Status                   | Client placed the request    |
| marriage   | Triple_Seal_Apostilles   | Stage                    | Order Received               |
| i9         | I_9_Verification         | Stage                    | Order Received               |
| quote      | Get_A_Quote_Leads        | GET_A_QUOTE_LEADS (!)    | Order Received               |

### CRITICAL: Get_A_Quote_Leads Module

- `GET_A_QUOTE_LEADS` is a **picklist field** (NOT a stage field like others)
- It serves as a **service type distribution** field (shows which service the lead is about)
- Options include: services like "FBI Apostille", "Embassy", etc.
- **"Phone Call Received" was added as a custom option** for unidentified phone leads
- When a form is submitted, it goes to a regular service option (e.g., selected services)
- When a phone lead can't be detected to any service → goes here with `GET_A_QUOTE_LEADS: "Phone Call Received"`

### CRITICAL: Deals (FBI) Module — `Stage` NOT `Status`

- Deals module has **`Stage`** (picklist) — NOT `Status`
- `zoho_sync.py` sends `"Status": "Order Received"` — this is LEGACY, Zoho accepts it via alias
- Phone lead code (`whatconverts_zoho.py`, `phone_lead_matcher.py`) MUST use `Stage`
- Confirmed via Zoho API: `/crm/v2/settings/fields?module=Deals` → field `api_name: "Stage"`

### Zoho Field Names Per Module — Quick Reference (for lead creation)

```
┌──────────────────────────┬────────────┬──────────────┬──────────────┬──────────────┬────────────────────┬───────────────────────┐
│ Module                   │ Deal/Name  │ Client Name  │ Email        │ Phone        │ Stage/Status       │ Comments              │
├──────────────────────────┼────────────┼──────────────┼──────────────┼──────────────┼────────────────────┼───────────────────────┤
│ Deals (FBI)              │ Deal_Name  │ Name1        │ Email_1      │ Phone        │ Stage              │ Client_Comment        │
│ Embassy_Legalization     │ Name       │ Client_Name  │ Email        │ Phone        │ Status             │ Client_Comment        │
│ Translation_Services     │ Name       │ Client_Name1 │ Email        │ Phone        │ Translation_Status │ Client_Comments       │
│ Apostille_Services       │ Name       │ Client_Name  │ Email        │ Phone_Number │ Status             │ Client_Comments       │
│ Triple_Seal_Apostilles   │ Name       │ Client_Name  │ Client_Email │ Client_Phone │ Stage              │ Client_Notes_Comments │
│ I_9_Verification         │ Name       │ Client_Name  │ Client_Email │ Client_Phone │ Stage              │ Client_Comments       │
│ Get_A_Quote_Leads        │ Name       │ Client_Name  │ Client_Email │ Client_Phone │ GET_A_QUOTE_LEADS  │ Client_Comments       │
└──────────────────────────┴────────────┴──────────────┴──────────────┴──────────────┴────────────────────┴───────────────────────┘
```

### Zoho Field Names for UPDATE (phone_lead_matcher.py → update_zoho_lead_with_order_data)

When updating an existing Zoho record with form data, name/email field names differ:

| Module                 | Name Field   | Email Field    |
|------------------------|-------------|----------------|
| Deals                  | Name1       | Email_1        |
| Translation_Services   | Client_Name1| Email          |
| Triple_Seal_Apostilles | (default)   | Client_Email   |
| I_9_Verification       | (default)   | Client_Email   |
| Get_A_Quote_Leads      | (default)   | Client_Email   |
| Others (Embassy, etc.) | Client_Name | Email          |

---

## Zoho CRM Field Reference (Verified from API — Feb 2026)

Complete field mapping for all modules. Verified via `/crm/v2/settings/fields` API.
Only custom/business fields listed (system fields like Created_By, Tag omitted).

### Deals (FBI) — 46 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Deal_Name | Potential Name | text | Record name (required) |
| Stage | Stage | picklist | **THE stage field** (NOT Status!) |
| Name1 | Client Name | text | |
| Email_1 | Client Email | email | |
| Phone | Client Phone | phone | |
| Amount | Amount | currency | |
| Address | Address | text | |
| Country_of_Use | Country of Use | text | |
| Client_Comment | Client Comment | textarea | |
| Order_ID | Order ID | autonumber | |
| Package | Package | picklist | Expedited/Standard/State-Level |
| Certificate | Certificates | picklist | 1-6 |
| Shipping_speed | Shipping | picklist | UPS Ground, 1-Day, 2-Day, FedEx, DHL, Intl |
| Payment_Status | Payment Status | picklist | Not Paid, Deposit Paid, Fully Paid |
| Submission_Date | Submission Date | date | |
| Client_Contact | Client Contact | lookup | |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | → Lead_Attribution_Records |
| Detected_Country | Detected Country | text | |
| Our_comment | Our comment | textarea | |
| Translation_Required | Translation Required | picklist | Yes/No |
| Translation_Options | Translation Options | picklist | Wet Signatures/Digital |
| Translation_Payment_Status | Translation Payment Status | picklist | |
| UPS_Tracking_Number | UPS Tracking Number | text | |

**Stage picklist values:** FROM APOSTILLE REQUEST, Order Received, Pending Submission, Order Submission Stage, State Department Submission, Pick-Up of Documents, UPS label generated, Resubmissions, Rejected, Send Review, No Review, Documents Dropped Off, Under Translation, No Label, Fully Refunded, Notarization, Court, Secretary of State, USDOS, Translation, Embassy, UPS/FedEX/DHL drop off, Delivery and Reviews, **Phone Call Received**

### Embassy_Legalization — 41 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| Status | Lead Status | picklist | **THE stage field** |
| Client_Name | Client Name | text | |
| Email | Email | email | |
| Phone | Phone | text | |
| Address | Address | text | |
| Country_of_Legalization | Country of Legalization | text | |
| Document_Type | Document Type | text | |
| Client_Comment | Client Comment | textarea | |
| Payment_Status | Payment Status | picklist | Not Paid, Deposit, Fully Paid |
| Amount_Paid | Amount Paid | currency | |
| Process_Stage | Process Stage | picklist | Separate field! |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**Status picklist values:** Phone Call Received, Order Received, EMBASSY LEADS FROM GET QUOTE PIPELINE, Initial Contact Made, Call Follow Up, Email 1/2 Follow Up, Client Lost, In Progress, State Authentication, Federal Authentication, Embassy/Consulate Legalization, Shipping, No Review, Completed, Cancelled, Rejected

### Translation_Services — 43 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| Translation_Status | Translation Status | picklist | **THE stage field** |
| Client_Name1 | Client Name | text | NOTE: Client_Name1 (not Client_Name!) |
| Client_Name | Contact | lookup | This is a LOOKUP, not text! |
| Email | Email | email | |
| Phone | Phone | phone | |
| Languages | Languages | text | |
| Client_Comments | Client Comments | textarea | |
| Client_Address | Client Address | text | NOTE: not "Address" |
| Number_of_Document | Number of Document | text | |
| Payment_Status | Payment Status | picklist | |
| Price_Per_Page | Amount Paid | currency | |
| Translation_Stages | Translation Stages | picklist | Separate from Translation_Status! |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**Translation_Status picklist values:** Phone Call Received, Client Placed Request, TRANSLATION LEADS FROM GET QUOTE PIPELINE, Initial Contact, Call Follow Up, Email 1/2 Follow Up, Client Lost, In Progress, Completed, Shipping, No Review, Completed (Send Review), Cancelled, FROM FBI TRANSLATION REQUESTS

**⚠️ GOTCHA**: `Client_Name` (lookup) vs `Client_Name1` (text). For creating/updating records, use **Client_Name1** for the client name string.

### Apostille_Services — 39 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| Status | Status | picklist | **THE stage field** |
| Client_Name | Client Name | text | |
| Email | Email | email | For creation |
| Client_Email | Client Email | email | Also exists! |
| Phone_Number | Phone Number | phone | NOTE: Phone_Number (not Phone!) |
| Address | Address | text | |
| Country_of_Use | Country of Use | text | |
| Document_Type | Document Type | text | |
| Client_Comments | Client Comments | textarea | |
| Number_of_copies | Number of copies | text | |
| Process_Stage | Process Stage | picklist | Separate from Status! |
| Amount_Paid | Amount Paid | currency | |
| Payment_Status | Payment Status | picklist | |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**Status picklist values:** Client placed the request, Phone Call Received, APOSTILE LEADS FROM GET QUOTE PIPELINE, Initial Contact Made, Call Follow up, Email 1/2 Follow Up, FBI - Federal Apostille Requests, ALL TRIPLE SEAL ORDERS, Client Lost, In Progress, Shipping/drop off, No Review, Completed, Rejected, Cancelled

**⚠️ GOTCHA**: Has BOTH `Email` and `Client_Email` fields. Phone lead code uses `Email`, zoho_update uses `Email`.

### Triple_Seal_Apostilles (Marriage) — 39 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| Stage | Stage | picklist | **THE stage field** |
| Client_Name | Client Name | text | |
| Client_Email | Client Email | email | |
| Client_Phone | Client Phone | phone | |
| Client_Address | Client Address | text | NOTE: Client_Address (not Address!) |
| Client_Notes_Comments | Client Notes/Comments | textarea | |
| Marriage_Info | Marriage Info | textarea | |
| Type_of_Legalization | Type of Legalization | picklist | Triple Seal/Apostille/Embassy |
| Amount_Paid | Amount Paid | currency | |
| Payment_Status | Payment Status | picklist | |
| Number_of_Copies | Number of Copies | integer | |
| Client_Contact | Client Contact | lookup | |
| Linked_Deal | Linked Deal | lookup | |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**Stage picklist values:** Phone Call Received, Order Received, Initial Contact Made, Call Follow up, Email 1/2 Follow Up, Client Lost, In Progress (Submitted to Authorities), Shipping/drop off, No Review, Completed, TRIPLE SEAL ORDERS FROM APOSTILLE, Rejected, Cancelled

### I_9_Verification — 32 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| Stage | Stage | picklist | **THE stage field** |
| Client_Name | Client Name | text | |
| Client_Email | Client Email | email | |
| Client_Phone | Client Phone | phone | |
| Address | Address | text | |
| Form_Date_Time | Form Date/Time | text | |
| Client_Comments | Client Comments | textarea | |
| Total_Paid | Total Paid | currency | |
| Client_Contact | Client Contact | lookup | |
| Tracking_ID | Tracking ID | text | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**Stage picklist values:** Phone Call Received, Order Received, I-9 LEADS FROM GET QUOTE PIPELINE, Initial Contact Made, Call Follow up, Email 1/2 Follow Up, To get Back Later, Client Lost, In Progress, Shipping/drop off, No Review, Completed

### Get_A_Quote_Leads — 26 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Record name |
| GET_A_QUOTE_LEADS | GET A QUOTE LEADS | picklist | Service type / phone lead indicator |
| Client_Name | Client Name | text | |
| Client_Email | Client Email | email | |
| Client_Phone | Client Phone | phone | |
| Client_Address_Location | Client Address/Location | text | NOTE: different from other modules! |
| Client_Comments | Client Comments | textarea | |
| Number_Of_Documents | Number Of Documents | integer | |
| Date_Time | Date/Time | text | |
| Document_Type | Document Type | text | |
| Stage | Stage | text | Plain text, NOT picklist |
| Country_of_Use | Country of Use | text | |
| Name_of_Client | Client Contact | lookup | |
| Attribution_Record | Attribution Record | lookup | |
| Detected_Country | Detected Country | text | |

**GET_A_QUOTE_LEADS picklist values:** Phone Call Received, Translation Service, Notary Service, I-9 Employment Verification Services, Apostille Service, Embassy Legalization Services

### Lead_Attribution_Records — 32 fields total

| API Name | Label | Type | Notes |
|----------|-------|------|-------|
| Name | CustomModule Name | text | Auto-generated: "ClientName \| source/medium \| date" |
| Source | Source | text | UTM source |
| Medium | Medium | text | UTM medium |
| Campaign | Campaign | text | UTM campaign |
| UTM_Content | UTM Content | text | |
| UTM_Term | UTM Term | text | |
| GCLID | GCLID | text | Google Click ID |
| FBCLID | FBCLID | text | Facebook Click ID |
| MSCLKID | MSCLKID | text | Microsoft Click ID |
| Landing_Page | Landing Page | text | |
| Lead_URL | Lead URL | text | |
| Referrer_Domain | Referrer Domain | text | |
| Device_Type | Device Type | picklist | Mobile, Desktop, Tablet, Other |
| Lead_Type | Lead Type | picklist | Phone Call, Form Submission, Chat, Email, Manual |
| Call_Duration | Call Duration | integer | Seconds |
| Call_Recording_URL | Call Recording URL | website | |
| City | City | text | |
| State | State | text | |
| Country | Country | text | |
| Visit_Count | Visit Count | integer | |
| Pages_Viewed | Pages Viewed | integer | |
| First_Visit_At | First Visit At | datetime | |
| Created_at | Created at | datetime | |

### Stage/Status Field Name → API Name Mapping (CRITICAL)

This is the **single source of truth** for which field controls the pipeline stage in each module:

```python
STAGE_FIELD_MAP = {
    'Deals':                'Stage',               # NOT Status!
    'Embassy_Legalization': 'Status',
    'Translation_Services': 'Translation_Status',
    'Apostille_Services':   'Status',
    'Triple_Seal_Apostilles': 'Stage',
    'I_9_Verification':     'Stage',
    'Get_A_Quote_Leads':    'GET_A_QUOTE_LEADS',   # picklist, not a real stage
}
```

Used in: `whatconverts_zoho.py`, `phone_lead_matcher.py`, `zoho_update.py`

### Address Field Name Variations

| Module | Address API Name |
|--------|-----------------|
| Deals | Address |
| Embassy_Legalization | Address |
| Translation_Services | Client_Address |
| Apostille_Services | Address |
| Triple_Seal_Apostilles | Client_Address |
| I_9_Verification | Address |
| Get_A_Quote_Leads | Client_Address_Location |

---

## Two Parallel Flows

### Flow 1: Web Form Submission (zoho_sync.py — LEGACY, DO NOT MODIFY)

```
Frontend Form → API View → Serializer → Order Created
  ↓
  process_attribution(request, order)
    → extract_attribution_from_request() OR phone lead attribution
    → check_and_update_phone_lead() ← LINKS TO FLOW 2
  ↓
  save_file_attachments()
  ↓
  create_order_tracking() → Track with TID
  ↓
  sync_order_to_zoho_task.delay() → Celery
    → checks order.zoho_synced (if True → SKIP, prevents duplicates)
    → sync_*_order_to_zoho() from zoho_sync.py
    → creates Zoho record with proper stage (e.g., "Order Received")
  ↓
  send_staff_notification()
  send_tracking_email_task.delay()
```

**Form stages in zoho_sync.py** (hardcoded per function):
- FBI → `Status: "Order Received"` ← LEGACY uses "Status" but Zoho field is actually "Stage"
- Embassy → `Status: "Order Received"`
- Translation → `Translation_Status: "Client Placed Request"`
- Apostille → `Status: "Client placed the request"`
- Marriage → `Stage: "Order Received"`
- I-9 → `Stage: "Order Received"`
- Quote → no stage field set (uses `GET_A_QUOTE_LEADS` picklist with selected service)

### Flow 2: Phone Call (WhatConverts Webhook)

```
WhatConverts → POST /api/webhook/whatconverts/
  ↓
  webhooks.py filters:
    - Only "Phone Call" lead_type (skip form/chat)
    - Skip /tracking page leads
    - Skip spam
  ↓
  process_whatconverts_phone_lead(data)
    → parse_whatconverts_webhook(data)
    → detect_service_from_url(landing_url) → (service, zoho_module)
    → find_matching_order(phone, email, service_type) ← checks SAME service pipeline only
      → If order found: return None (90% clarification call, skip lead)
    → find_duplicate_phone_lead(phone, email, service_type)
      → If dup found: UPDATE existing (preserve Zoho IDs)
      → If no dup: CREATE new PhoneCallLead
  ↓
  sync_phone_lead_to_zoho(phone_lead)
    → Guard: if zoho_synced AND zoho_lead_id → SKIP (prevent duplicate on repeat calls)
    → build_zoho_lead_payload(phone_lead) → module-specific fields
    → client.create_record(zoho_module, payload) → stage = "Phone Call Received"
    → create_attribution_record() → Lead_Attribution_Records + link to lead
```

---

## Phone Lead ↔ Form Matching (CRITICAL LOGIC)

### When Form Submitted After Phone Call

```
process_attribution(request, order)
  → check_and_update_phone_lead(order, request)
    → process_order_with_phone_lead_check(order, order_type, order_data)
```

**`process_order_with_phone_lead_check()` does:**

1. **Find matching phone lead** by phone number within same service pipeline
2. **Update PhoneCallLead** with form data (name, email, location)
3. **Set `order.zoho_synced = True`** — prevents Celery from creating DUPLICATE Zoho record
4. **Read current stage from Zoho** via `get_record_by_id()`
5. **Conditional stage advancement:**
   - If current stage == `"Phone Call Received"` → advance to module-specific "Order Received" equivalent
   - If current stage is ANYTHING ELSE → don't touch stage (manager may have already moved it further)
   - Always update contact data (name, email, location) regardless of stage

### Stage Advancement Safety

This prevents two problems:
- **Problem 1**: Phone lead at "In Progress" gets rolled back to "Order Received" when form comes in
- **Problem 2**: Phone lead stays stuck at "Phone Call Received" even after form is submitted

**Solution**: Read current stage from Zoho before updating. Only advance if still in initial "Phone Call Received" state.

### Module-specific "Form Stage" Values (`_get_form_stage()`)

```python
'Deals': 'Order Received'
'Embassy_Legalization': 'Order Received'
'Apostille_Services': 'Client placed the request'
'Translation_Services': 'Client Placed Request'
'Triple_Seal_Apostilles': 'Order Received'
'I_9_Verification': 'Order Received'
'Get_A_Quote_Leads': 'Order Received'
```

---

## Duplicate Prevention

### Phone Leads (repeat calls from same person)

- `find_duplicate_phone_lead()` — matches by phone/email within SAME service pipeline
- If duplicate found: UPDATE existing PhoneCallLead, PRESERVE Zoho IDs
- `sync_phone_lead_to_zoho()` has guard: `if zoho_synced and zoho_lead_id → SKIP`
- This prevents creating multiple Zoho records when same person calls repeatedly

### Form After Phone Call (zoho_synced flag)

- When form matches a phone lead with `zoho_lead_id`:
  - `order.zoho_synced = True` → Celery task checks this flag and SKIPS Zoho creation
  - Existing Zoho lead gets UPDATED (not duplicated)

### Cross-Service Isolation

- ALL matching is scoped to SAME service pipeline
- FBI phone lead does NOT match Embassy order even with same phone number
- This prevents cross-contamination between service pipelines

---

## Service Detection from URL

Landing URL patterns determine which Zoho module a phone lead goes to.

**Order matters!** More specific patterns before generic:
- `/apostille-fbi` → FBI (checked BEFORE `/apostille`)
- `/embassy-legalization` → Embassy
- `/translation-services` → Translation
- `/triple-seal-marriage` → Marriage
- `/i-9-verification-form` → I-9
- `/online-notary-form` → Notary (→ Get_A_Quote_Leads)
- `/apostille` → Apostille (LAST — generic pattern)
- No match → `Get_A_Quote_Leads` with `GET_A_QUOTE_LEADS: "Phone Call Received"`

---

## Attribution Records (Lead_Attribution_Records)

Zoho custom module storing marketing data for each lead.

**Fields:**
- Source, Medium, Campaign, UTM_Content, UTM_Term
- GCLID, FBCLID, MSCLKID
- Landing_Page, Lead_URL, Referrer_Domain
- Device_Type, City, State, Country
- Lead_Type ("Form Submission" or "Phone Call")
- Call_Duration, Call_Recording_URL (for phone leads)
- Pages_Viewed, Visit_Count, First_Visit_At
- Name (auto-generated: "ClientName | source/medium | date")

**Linking**: After creating attribution record, UPDATE the lead/deal record with:
```python
{'Attribution_Record': attribution_id}  # Lookup field ON the lead
```

**Two sources of attribution:**
1. **Web forms**: `DCMNTracker` JavaScript sends attribution in `request.data.attribution`
2. **Phone calls**: WhatConverts provides source/medium/campaign/gclid in webhook

---

## FBI & Marriage Orders (Stripe Payment Flow)

These two order types require payment BEFORE Zoho sync:

```
Form Submit → Order Created (NOT synced to Zoho yet)
  → process_attribution() only (saves attribution data)
  → save_file_attachments()
  ↓
Frontend redirects to Stripe Checkout
  ↓
Stripe webhook (checkout.session.completed)
  → order.is_paid = True
  → sync_order_to_zoho_task.delay(order_id, type, tracking_id)
  → send_tracking_email_task.delay(tid, 'created')
  → Send staff + client emails
```

Other order types (Embassy, Apostille, Translation, I-9, Quote) sync to Zoho immediately on form submission.

---

## ZohoCRMClient (zoho_client.py)

```python
client = ZohoCRMClient()
client.create_record(module, payload)    # POST — create new record
client.update_record(module, id, payload) # PUT — update existing record
client.create_attribution_record(payload) # Shortcut for Lead_Attribution_Records
```

Also used: `get_record_by_id()` from `zoho_sync.py` — reads current field values from Zoho.

---

## Celery Tasks

| Task | Purpose |
|------|---------|
| `sync_order_to_zoho_task` | Sync form order to Zoho (checks `zoho_synced` flag) |
| `send_tracking_email_task` | Send HTML tracking email (with retry + backoff) |
| `write_tracking_id_to_zoho_task` | Write TID to Zoho record |

`sync_order_to_zoho_task` checks `if not order.zoho_synced:` before calling sync function.
When phone lead sets `order.zoho_synced = True`, this task becomes a no-op.

---

## API Endpoints

| Endpoint | View | Purpose |
|----------|------|---------|
| `POST /api/fbi/create-order/` | CreateFbiOrderView | Create FBI order |
| `GET /api/fbi/options/` | FbiOptionsView | Get packages/shipping |
| `POST /api/marriage/create-order/` | CreateMarriageOrderView | Create marriage order |
| `POST /api/embassy/create-order/` | CreateEmbassyOrderView | Create embassy order |
| `POST /api/apostille/create-order/` | CreateApostilleOrderView | Create apostille order |
| `POST /api/translation/create-order/` | CreateTranslationOrderView | Create translation order |
| `POST /api/i9/create-order/` | CreateI9OrderView | Create I-9 order |
| `POST /api/quote/create-order/` | CreateQuoteRequestView | Create quote request |
| `POST /api/stripe/create-session/` | CreateStripeSessionView | Create Stripe checkout |
| `POST /api/stripe/webhook/` | stripe_webhook | Stripe payment webhook |
| `POST /api/webhook/whatconverts/` | whatconverts_webhook | WhatConverts phone leads |
| `POST /api/webhook/whatconverts-test/` | whatconverts_test_webhook | Test/debug webhook |

---

## Common Gotchas & Past Bugs

1. **`EmbassyLegalizationOrder` not `EmbassyOrder`** — import will fail
2. **`contact_phone` is NOT nullable** on PhoneCallLead — always provide default `''`
3. **URL ordering matters** — `/api/webhook/whatconverts-test/` BEFORE `/api/webhook/whatconverts/`
4. **`GET_A_QUOTE_LEADS` is a picklist field**, not a stage — don't confuse with `Stage` or `Status`
5. **Never modify zoho_sync.py form functions** — they are stable and tested
6. **`_get_stage_field()` in phone_lead_matcher.py** maps module → correct stage/status field name
7. **Attribution Record linking**: update the LEAD record with `Attribution_Record: id`, not the other way
8. **Quote → I-9 task type**: Celery task uses `"I-9"` (with dash), not `"i9"`
9. **Marriage Zoho module**: `Triple_Seal_Apostilles` (not `Marriage` or `Triple_Seal`)
10. **Phone lead contact_phone normalization**: last 10 digits only, for matching via `__icontains`
11. **Deals (FBI) stage field is `Stage` NOT `Status`** — verified via Zoho API. Legacy `zoho_sync.py` sends `Status` (works via Zoho alias), but all new code MUST use `Stage`
12. **Translation_Services has `Client_Name` (lookup) AND `Client_Name1` (text)** — use `Client_Name1` for string name
13. **Apostille_Services has BOTH `Email` and `Client_Email`** — use `Email` for creation, both exist
14. **Get_A_Quote_Leads `Stage` field is plain text (NOT picklist)** — don't confuse with other modules' Stage
15. **Triple_Seal address field is `Client_Address`** not `Address` — same for Translation_Services
16. **Get_A_Quote_Leads address is `Client_Address_Location`** — unique among all modules
17. **PhoneCallLead URLField max_length=500** — increased from default 200 (migration 0027)
