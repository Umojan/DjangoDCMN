# Django Backend - Полная бизнес-логика системы

## 📋 Содержание

1. [Источники лидов](#источники-лидов)
2. [Типы заказов](#типы-заказов)
3. [Основные потоки обработки](#основные-потоки-обработки)
4. [Интеграции](#интеграции)
5. [Дополнительные функции](#дополнительные-функции)
6. [Ключевые бизнес-правила](#ключевые-бизнес-правила)

---

## 🎯 Источники лидов

### 1. **Веб-формы (прямые заказы)**

Все формы отправляются через REST API:

| Endpoint | Сервис | Оплата |
|----------|--------|--------|
| `/fbi/create-order/` | FBI Apostille | ✅ Требуется |
| `/marriage/create-order/` | Marriage/Triple Seal | ✅ Требуется |
| `/embassy/create-order/` | Embassy Legalization | ❌ Бесплатно |
| `/apostille/create-order/` | State Apostille | ❌ Бесплатно |
| `/translation/create-order/` | Translation Service | ❌ Бесплатно |
| `/i9/create-order/` | I-9 Verification | ❌ Бесплатно |
| `/quote/create-order/` | Quote Request | ❌ Лид |

---

### 2. **WhatConverts (телефонные звонки)**

**Endpoint:** `/webhook/whatconverts/`

**Обработка:**
```
1. Фильтрация:
   ✅ Только lead_type = "Phone Call"
   ❌ Игнор /tracking страницы
   ❌ Игнор spam = true

2. Определение сервиса по URL:
   /apostille-fbi → FBI Apostille
   /triple-seal-marriage → Marriage
   /embassy-legalization → Embassy
   /translation-services → Translation
   / (homepage) → Get a Quote (fallback)

3. Создание PhoneCallLead:
   - Контактная информация
   - Метаданные звонка (длительность, запись)
   - AI анализ (sentiment, intent, keywords)
   - Attribution (source, medium, campaign, gclid)
   - Геолокация, устройство

4. Синхронизация с Zoho:
   - Создание Lead в нужном модуле
   - Stage: "Phone Call Received"
   - Lead Attribution Record
   - Привязка attribution к лиду
```

**Matching с веб-формами:**
- При создании заказа проверяется phone lead по номеру + service
- Если найден: обновляется контактная информация
- WhatConverts attribution используется вместо веб-формы
- Zoho lead обновляется на stage "Order Received"
- order.zoho_synced = True (предотвращает дубликат)

---

### 3. **Stripe (платежи)**

**Endpoint:** `/webhook/stripe/`

**События:**
- `checkout.session.completed` → оплата получена
- Триггерит синхронизацию с Zoho + email уведомления

---

### 4. **Zoho CRM (обратные вебхуки)**

**Endpoints:**
- `/tracking/crm/create/` → создать TID из CRM
- `/tracking/crm/update/` → обновить stage из CRM

---

## 📦 Типы заказов

### Платные заказы (требуют оплату через Stripe)

#### 1. **FbiApostilleOrder** - FBI Background Check Apostilles
```python
Поля:
├─ Package (FbiServicePackage): standard/rush/super_rush
├─ Count: количество сертификатов
├─ Shipping (ShippingOption): Mail/UPS/FedEx/etc
├─ Total price = package + shipping + (count × per_cert_price)
├─ Files: FBI document attachments
├─ is_paid: False до оплаты, True после
├─ zoho_synced: False до оплаты, True после
└─ attribution_data: маркетинговая атрибуция

Flow:
1. Форма → Order создан (is_paid=False)
2. Stripe session → TID создан
3. Оплата → is_paid=True
4. Webhook → Zoho sync + TID запись + Emails
```

#### 2. **MarriageOrder** - Triple Seal Marriage Certificates
```python
Поля:
├─ Husband/Wife names
├─ Marriage date, country
├─ Certificate number OR file upload
├─ Shipping option
├─ Fixed base price (MarriagePricingSettings)
└─ Аналогичный flow с FBI
```

---

### Бесплатные заказы (без оплаты)

#### 3. **EmbassyLegalizationOrder** - Embassy Legalization
```python
Поля:
├─ Document type, country, notarization
├─ Files attached
├─ Comments
└─ attribution_data

Flow:
1. Форма → Order создан
2. Сразу: TID создан + Zoho sync + Emails
```

#### 4. **ApostilleOrder** - State Apostilles
```python
Similar to Embassy, но для State Apostille сервиса
```

#### 5. **TranslationOrder** - Translation Services
```python
Поля:
├─ Original language → Target language
├─ Document type
├─ Files
└─ Special instructions
```

#### 6. **I9VerificationOrder** - I-9 Verification
```python
Поля:
├─ Appointment date/time
├─ Services (remote/in-person)
├─ Comments
└─ Notarization required?
```

#### 7. **QuoteRequest** - Quote Leads
```python
Поля:
├─ Services requested (multi-select)
├─ Appointment date/time
├─ Comments
└─ Не полноценный заказ, просто лид
```

---

### Специальная модель

#### 8. **PhoneCallLead** - WhatConverts Phone Calls
```python
Поля:
├─ whatconverts_lead_id (unique)
├─ Contact: name, email, phone, company
├─ Call: duration, recording_url, lead_score
├─ Service: detected_service, landing_url
├─ Attribution: source, medium, campaign, gclid
├─ Location: city, state, zip, country
├─ Device: type, make, OS, browser
├─ AI: lead_summary, sentiment, intent, keywords
├─ Zoho: zoho_lead_id, zoho_attribution_id, zoho_module
├─ Matching: matched_with_form, matched_order_type/id
└─ raw_webhook_data (полный JSON)

Особенности:
- Создается из WhatConverts webhook
- Может быть matched с веб-формой позже
- Attribution сохраняется при matching
```

---

## 🔄 Основные потоки обработки

### FLOW 1: Бесплатный заказ (Embassy, Apostille, Translation, I-9)

```
┌─────────────────────────────────────────────────────────┐
│ 1. Frontend POST /embassy/create-order/                │
│    ├─ Validation (DRF Serializer)                      │
│    └─ Order created in DB                              │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 2. process_new_order(order, create_tid, sync_zoho...)  │
│    ├─ process_attribution(request, order)              │
│    │  ├─ check_and_update_phone_lead()                 │
│    │  │  └─ Если phone lead найден:                    │
│    │  │     ├─ Используется WhatConverts attribution   │
│    │  │     ├─ phone_lead обновлен данными формы       │
│    │  │     └─ order.zoho_synced = True                │
│    │  └─ Иначе: extract_attribution_from_request()     │
│    │     └─ Берется attribution из формы               │
│    │                                                    │
│    ├─ save_file_attachments(request, order)            │
│    │  └─ GenericForeignKey → FileAttachment            │
│    │                                                    │
│    ├─ create_tracking_record(order, service_type)      │
│    │  ├─ Generate unique TID (20 chars)                │
│    │  ├─ Create Track object                           │
│    │  ├─ order.track = track                           │
│    │  └─ order.tid_created = True                      │
│    │                                                    │
│    ├─ Queue Zoho sync (Celery)                         │
│    │  └─ sync_order_to_zoho_task.delay(order_id,       │
│    │                                     order_type,    │
│    │                                     tracking_id)   │
│    │     ├─ Get/Create Contact in Zoho                 │
│    │     ├─ Create Deal/Lead in module                 │
│    │     ├─ If attribution_data exists:                │
│    │     │  ├─ Create Lead_Attribution_Record          │
│    │     │  └─ Link to order (Attribution_Record)      │
│    │     ├─ Upload file attachments                    │
│    │     ├─ Write Tracking_ID to Zoho                  │
│    │     └─ order.zoho_synced = True                   │
│    │                                                    │
│    ├─ send_staff_notification(order, order_type)       │
│    │  ├─ Subject: "📄 New Embassy Order — 2026-02-04"  │
│    │  ├─ Recipient: EMAIL_OFFICE_RECEIVER              │
│    │  ├─ Body: order details + file links              │
│    │  └─ Threading: by type + date                     │
│    │                                                    │
│    └─ Queue tracking email (Celery)                    │
│       └─ send_tracking_email_task.delay(tid,           │
│                                          'created')     │
│          ├─ Subject: "Your Order Status"               │
│          ├─ Body: "Order Received 📋" + tracking link  │
│          └─ Retry 5x with exponential backoff          │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 3. API Response                                         │
│    ├─ order_id                                          │
│    ├─ tracking_id (TID)                                 │
│    └─ file_urls                                         │
└─────────────────────────────────────────────────────────┘
```

---

### FLOW 2: Платный заказ (FBI, Marriage)

```
┌─────────────────────────────────────────────────────────┐
│ 1. Frontend POST /fbi/create-order/                    │
│    ├─ Order created (is_paid=False)                    │
│    ├─ process_attribution() called                     │
│    ├─ save_file_attachments() called                   │
│    └─ API returns: {order_id}                          │
│                                                         │
│    ⚠️ NO TID, NO Zoho sync, NO emails yet!             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Frontend POST /create-stripe-session/               │
│    ├─ Validate order exists & unpaid                   │
│    ├─ Create TID EARLY (before payment!)               │
│    │  ├─ Generate TID                                  │
│    │  ├─ Create Track object                           │
│    │  ├─ order.track = track                           │
│    │  └─ order.tid_created = True                      │
│    │                                                    │
│    ├─ Create Stripe Session                            │
│    │  ├─ amount = order.total_price * 100 (cents)      │
│    │  ├─ metadata = {order_id, order_type, tid}        │
│    │  └─ success_url, cancel_url                       │
│    │                                                    │
│    └─ API returns: {checkout_url}                      │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Customer pays on Stripe                             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Stripe webhook POST /webhook/stripe/                │
│    Event: checkout.session.completed                   │
│    ├─ Extract metadata: order_id, order_type, tid      │
│    ├─ Route to _handle_fbi_payment() or                │
│    │            _handle_marriage_payment()             │
│    │                                                    │
│    └─ Handler:                                          │
│       ├─ order.is_paid = True                          │
│       ├─ order.save()                                  │
│       │                                                 │
│       ├─ Queue Zoho sync WITH tracking_id              │
│       │  └─ sync_order_to_zoho_task.delay(order_id,    │
│       │                                    order_type,  │
│       │                                    tid)         │
│       │                                                 │
│       ├─ Queue tracking email                          │
│       │  └─ send_tracking_email_task.delay(tid,        │
│       │                                     'created')  │
│       │                                                 │
│       ├─ Send manager notification                     │
│       │  ├─ Subject: "✅ New PAID FBI Order"           │
│       │  ├─ Includes: "Payment: ✅ Received"           │
│       │  └─ order.manager_notified = True              │
│       │                                                 │
│       └─ Send client confirmation email                │
│          └─ Template: emails/fbi_order_paid.html       │
│                                                         │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 5. Customer receives:                                   │
│    ├─ Stripe payment receipt                           │
│    ├─ Welcome tracking email (with TID)                │
│    └─ Can access /tracking/{tid}/                      │
└─────────────────────────────────────────────────────────┘
```

**Ключевое отличие:**
- TID создается **ДО оплаты** (при создании Stripe session)
- Zoho sync **ПОСЛЕ оплаты** (из webhook)
- TID передается в Zoho вместе с заказом

---

### FLOW 3: WhatConverts Phone Call → Zoho Lead

```
┌─────────────────────────────────────────────────────────┐
│ 1. Customer calls tracked number                       │
│    (WhatConverts tracking system)                      │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 2. WhatConverts webhook POST /webhook/whatconverts/    │
│    Payload:                                             │
│    ├─ lead_id (WhatConverts unique ID)                 │
│    ├─ contact_name, contact_phone, contact_email       │
│    ├─ call_duration, lead_score, sentiment             │
│    ├─ lead_analysis (AI summary, intent, keywords)     │
│    ├─ landing_url, lead_url                            │
│    ├─ lead_source, lead_medium, lead_campaign, gclid   │
│    ├─ city, state, zip, country                        │
│    ├─ device_type, browser, operating_system           │
│    └─ call_recording_url                               │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Фильтрация                                           │
│    ├─ ✅ lead_type == "Phone Call"                      │
│    ├─ ❌ Skip if "/tracking" in landing_url             │
│    └─ ❌ Skip if spam == true                           │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 4. process_whatconverts_phone_lead(data)               │
│    ├─ detect_service_from_url(landing_url)             │
│    │  ├─ /apostille-fbi → "fbi" → FBI_Apostille       │
│    │  ├─ /triple-seal-marriage → "marriage"            │
│    │  ├─ /embassy-legalization → "embassy"             │
│    │  ├─ / (homepage) → None → Get_a_Quote (fallback)  │
│    │  └─ Map to zoho_module                            │
│    │                                                    │
│    ├─ Check for duplicate                              │
│    │  ├─ Search by whatconverts_lead_id (primary)      │
│    │  ├─ Fallback: search by phone/email               │
│    │  │  └─ Normalize phone (last 10 digits)           │
│    │  └─ If found: update existing record              │
│    │                                                    │
│    ├─ Create/Update PhoneCallLead                      │
│    │  ├─ All contact info                              │
│    │  ├─ Call metadata                                 │
│    │  ├─ detected_service, zoho_module                 │
│    │  ├─ AI analysis fields                            │
│    │  ├─ raw_webhook_data (full JSON)                  │
│    │  └─ matched_with_form = False (initially)         │
│    │                                                    │
│    └─ Check for matching web form order                │
│       ├─ Search all order models by phone/email        │
│       └─ If found:                                      │
│          ├─ phone_lead.matched_with_form = True        │
│          ├─ matched_order_type + matched_order_id      │
│          └─ Log: "Manager should update to 'Order      │
│             Received'"                                  │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 5. sync_phone_lead_to_zoho(phone_lead)                 │
│    ├─ Build lead payload:                              │
│    │  ├─ First_Name, Last_Name (split from name)       │
│    │  ├─ Phone, Email, Company                         │
│    │  ├─ Lead_Status = "Phone Call Received"           │
│    │  ├─ Lead_Source from source field                 │
│    │  ├─ Rating from lead_score (Hot/Warm/Cold)        │
│    │  ├─ Description from AI analysis                  │
│    │  └─ City, State, Zip, Country                     │
│    │                                                    │
│    ├─ Create Lead in Zoho module                       │
│    │  ├─ Module: FBI_Apostille / Marriage / etc.       │
│    │  │           OR Get_a_Quote (fallback)            │
│    │  └─ phone_lead.zoho_lead_id = lead ID             │
│    │                                                    │
│    ├─ Create Lead Attribution Record                   │
│    │  ├─ Name: "Contact | source/medium | datetime"    │
│    │  ├─ Source, Medium, Campaign, Keyword             │
│    │  ├─ Landing_Page, Lead_URL                        │
│    │  ├─ Device_Type, Browser, OS                      │
│    │  ├─ City, State, Country                          │
│    │  ├─ GCLID (Google Click ID)                       │
│    │  ├─ Lead_Type = "Phone"                           │
│    │  ├─ Call_Duration, Call_Recording_URL             │
│    │  ├─ First_Visit_At (call timestamp)               │
│    │  └─ Attribution_Record → zoho_lead_id (lookup)    │
│    │                                                    │
│    └─ phone_lead.zoho_synced = True                    │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 6. Webhook Response                                     │
│    ├─ phone_lead_id                                     │
│    ├─ zoho_lead_id                                      │
│    ├─ zoho_attribution_id                               │
│    ├─ detected_service                                  │
│    └─ matched_with_form (T/F)                           │
└─────────────────────────────────────────────────────────┘

Результат в Zoho:
├─ Lead в правильном модуле (FBI/Marriage/Get_a_Quote)
├─ Stage: "Phone Call Received"
├─ Lead Attribution Record создан и привязан
└─ Полная информация о звонке + attribution данные
```

---

### FLOW 4: Phone Call + Web Form (Matching)

```
┌─────────────────────────────────────────────────────────┐
│ Сценарий:                                               │
│ 1. Клиент звонит (10:00) → Phone Lead создан в Zoho    │
│ 2. Клиент заполняет форму (14:00) → Matching!          │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 1: Phone Call (уже произошло)                     │
│    ├─ PhoneCallLead #123 создан                        │
│    ├─ phone: "+1-555-123-4567"                         │
│    ├─ detected_service: "fbi"                           │
│    ├─ source: "google", gclid: "xyz123"                │
│    ├─ Zoho: Lead #5634000000123456                     │
│    └─ Stage: "Phone Call Received"                     │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 2: Web Form Submission                            │
│    POST /fbi/create-order/                             │
│    ├─ name: "John Doe"                                 │
│    ├─ phone: "+1-555-123-4567" ← SAME!                │
│    ├─ email: "john@example.com"                        │
│    └─ Order created                                     │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 3: process_attribution(request, order)            │
│    └─ check_and_update_phone_lead(order, request)      │
│       ├─ order_type = "fbi" (from class name)          │
│       ├─ order_data = {name, email, phone, ...}        │
│       │                                                 │
│       └─ process_order_with_phone_lead_check()         │
│          ├─ find_phone_lead_for_order(phone, "fbi")    │
│          │  ├─ Normalize phone: last 10 digits         │
│          │  ├─ Query: phone + service = "fbi"          │
│          │  └─ Found: PhoneCallLead #123 ✅            │
│          │                                              │
│          ├─ update_phone_lead_with_form_data()         │
│          │  ├─ phone_lead.contact_name = "John Doe"    │
│          │  ├─ phone_lead.contact_email = "john@..."   │
│          │  ├─ phone_lead.matched_with_form = True     │
│          │  ├─ phone_lead.matched_order_id = order.id  │
│          │  ├─ PRESERVE WhatConverts data:             │
│          │  │  ├─ source: "google" (unchanged)         │
│          │  │  ├─ gclid: "xyz123" (unchanged)          │
│          │  │  ├─ sentiment, intent (unchanged)        │
│          │  │  └─ call_recording_url (unchanged)       │
│          │  └─ phone_lead.save()                       │
│          │                                              │
│          ├─ order.zoho_synced = True ← ВАЖНО!          │
│          │  └─ Предотвращает создание дубликата        │
│          │                                              │
│          └─ update_zoho_lead_with_order_data()         │
│             ├─ Update Zoho Lead #5634000000123456:     │
│             │  ├─ First_Name: "John"                   │
│             │  ├─ Last_Name: "Doe"                     │
│             │  ├─ Email: "john@example.com"            │
│             │  ├─ Stage: "Order Received" ← UPGRADE!   │
│             │  └─ City, State, Country (from form)     │
│             └─ Same lead, not duplicate! ✅            │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 4: Attribution используется из Phone Lead         │
│    ├─ build_attribution_from_phone_lead(phone_lead)    │
│    │  ├─ source: "google" (from WhatConverts)          │
│    │  ├─ medium: "cpc" (from WhatConverts)             │
│    │  ├─ campaign: "..." (from WhatConverts)           │
│    │  ├─ gclid: "xyz123" (from WhatConverts)           │
│    │  ├─ lead_type: "phone" (not "form"!)              │
│    │  └─ All other phone lead data                     │
│    │                                                    │
│    └─ order.attribution_data = phone_lead attribution  │
│       (NOT form attribution!)                           │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ STEP 5: Celery Task sync_order_to_zoho_task            │
│    ├─ Checks: order.zoho_synced == True                │
│    └─ SKIPS sync (lead already exists!) ✅             │
│       No duplicate created!                             │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ Результат:                                              │
│ ✅ 1 лид в Zoho (обновлен, не дубликат)                 │
│ ✅ Stage: "Order Received"                              │
│ ✅ Контактные данные из формы                           │
│ ✅ Attribution из WhatConverts (сохранен!)              │
│ ✅ order.attribution_data имеет gclid, lead_score, etc  │
│ ✅ Полный customer journey: ad → call → form            │
└─────────────────────────────────────────────────────────┘
```

**Ключевые моменты:**
1. **Matching только в том же пайплайне**: FBI phone → FBI form
2. **WhatConverts attribution сохраняется**: source, gclid, etc
3. **order.zoho_synced = True** предотвращает дубликат
4. **Zoho lead обновляется**, не создается новый
5. **Attribution Record уже существует** из phone call

---

## 🔌 Интеграции

### 1. Zoho CRM

**Модули в Zoho:**

| Django Model | Zoho Module | Stage (initial) |
|--------------|-------------|-----------------|
| FbiApostilleOrder | Deals (legacy) / FBI_Apostille | Order Received |
| MarriageOrder | Triple_Seal_Apostilles | Order Received |
| EmbassyLegalizationOrder | Embassy_Legalization | Order Received |
| ApostilleOrder | Apostille_Services | Order Received |
| TranslationOrder | Translation_Services | Order Received |
| I9VerificationOrder | I_9_Verification | Order Received |
| QuoteRequest | Get_A_Quote_Leads | New |
| PhoneCallLead | (detected module) | Phone Call Received |

**Phone Lead Stage:** "Phone Call Received" → "Order Received" (при matching)

**Синхронизируемые данные:**
```python
{
    # Contact
    'First_Name': ...,
    'Last_Name': ...,
    'Email': ...,
    'Phone': ...,
    'Company': ...,

    # Order specifics (per module)
    'Service_Type': ...,
    'Document_Type': ...,
    'Country': ...,

    # Payment (for paid orders)
    'Total_Price': ...,
    'Payment_Status': 'Paid' / 'Pending',

    # Tracking
    'Tracking_ID': tid,

    # Attribution (via lookup)
    'Attribution_Record': attribution_record_id,

    # Files
    # Uploaded as attachments linked to record
}
```

**Lead Attribution Record:**
```python
{
    'Name': 'John Doe | google/cpc | 2026-02-04 15:30',
    'Source': 'google',
    'Source_Category': 'Google',  # Normalized
    'Medium': 'cpc',
    'Campaign': 'fbi apostille 2026',
    'Keyword': 'fbi apostille near me',
    'Landing_Page': 'https://dcmn.us/apostille-fbi',
    'Lead_URL': 'https://dcmn.us/apostille-fbi-form',
    'Referrer_Domain': 'google.com',
    'Device_Type': 'Mobile',
    'Browser': 'Chrome',
    'Pages_Viewed': 5,
    'Visit_Count': 2,
    'First_Visit_At': '2026-02-04T14:30:00Z',
    'City': 'Los Angeles',
    'State': 'CA',
    'Country': 'US',
    'GCLID': 'abc123...',
    'Lead_Type': 'Form' or 'Phone',
    'Call_Duration': 120,  # только для phone
    'Call_Recording_URL': 'https://...',  # только для phone
    'Attribution_Record': order_id,  # Lookup к заказу
}
```

**API Client:** `ZohoCRMClient`
- OAuth 2.0 с refresh token
- Access token кэшируется (50 мин TTL)
- Retry logic: 2 попытки с обновлением токена
- Rate limiting handling

---

### 2. Stripe

**События обрабатываются:**
- `checkout.session.completed` → оплата получена

**Метаданные session:**
```python
{
    'order_id': order.id,
    'order_type': 'fbi' or 'marriage',
    'tracking_id': tid,
}
```

**Webhook обработка:**
```python
def _handle_fbi_payment(session):
    order = FbiApostilleOrder.objects.get(id=order_id)
    order.is_paid = True
    order.save()

    # Queue Zoho sync
    sync_order_to_zoho_task.delay(order_id, 'fbi', tracking_id)

    # Queue tracking email
    send_tracking_email_task.delay(tracking_id, 'created')

    # Send manager notification
    send_staff_notification(order, 'fbi', extra='Payment: ✅')
    order.manager_notified = True

    # Send client confirmation
    send_client_payment_confirmation(order)
```

---

### 3. Email (Resend API)

**Типы писем:**

#### Staff Notification
```python
To: settings.EMAIL_OFFICE_RECEIVER
Subject: "{emoji} New {Service} Order — YYYY-MM-DD"
Thread: Group by service + date
Body:
  - Order details
  - Client info
  - File links
  - Comments
  - Payment status (if paid)
```

#### Tracking Welcome Email
```python
To: client email
Subject: "Your Order Status - {Service}"
Thread: Created by TID
Body:
  - "Order Received 📋"
  - Tracking link: /tracking?tid={tid}
  - Redacted name for privacy
```

#### Stage Update Emails
```python
To: client email
Subject: "Order Status Update"
Thread: Reply to welcome email (by TID)
Body:
  - Stage name + emoji
  - Message per stage
  - Tracking link
```

**Retry Logic:**
- 5 retries максимум
- Exponential backoff: 3s, 6s, 12s, 24s, 48s
- Via Celery task

**Threading:**
- Message-ID: `<tracking-{tid}@dcmn.us>`
- In-Reply-To + References для thread grouping

---

### 4. WhatConverts

**Webhook:** `POST /webhook/whatconverts/`

**Payload fields используемые:**
```python
{
    'lead_id': unique ID,
    'lead_type': 'Phone Call',
    'contact_name': ...,
    'contact_phone_number': ...,
    'contact_email_address': ...,
    'contact_company_name': ...,

    # Call specifics
    'call_duration': seconds,
    'call_recording_url': ...,
    'lead_score': 0-100,
    'lead_status': 'Unique' / 'Duplicate',
    'lead_state': 'Completed',

    # AI Analysis
    'lead_analysis': {
        'Keyword Detection': ...,
        'Lead Summary': ...,
        'Intent Detection': ...,
        'Sentiment Detection': 'Positive/Negative/Neutral',
        'Topic Detection': ...,
    },

    # Attribution
    'landing_url': where they came from,
    'lead_url': page they submitted on,
    'lead_source': 'google',
    'lead_medium': 'cpc',
    'lead_campaign': ...,
    'lead_keyword': ...,
    'gclid': Google Click ID,
    'msclkid': Microsoft Click ID,
    'fbclid': Facebook Click ID,

    # Location
    'city': ...,
    'state': ...,
    'zip': ...,
    'country': ...,
    'ip_address': ...,

    # Device
    'device_type': 'Smartphone/Tablet/Desktop',
    'device_make': 'Apple iPhone',
    'operating_system': 'iOS 16',
    'browser': 'Safari Mobile',

    # Timestamps
    'date_created': ISO datetime,
    'last_updated': ISO datetime,

    # Flags
    'duplicate': false,
    'spam': false,
}
```

**Service Detection Patterns:**
```python
'/apostille-fbi' → fbi → FBI_Apostille
'/triple-seal-marriage' → marriage → Marriage_Orders
'/embassy-legalization' → embassy → Embassy_Legalization
'/translation-services' → translation → Translation_Services
'/apostille' → apostille → Apostille_Orders
'/i-9' → i9 → I9_Verification
'/online-notary-form' → notary → Notary_Services
'/' or unknown → None → Get_a_Quote (fallback)
```

---

## ⚙️ Дополнительные функции

### 1. Tracking ID System

**Генерация:**
```python
def generate_tid() -> str:
    # Returns 20-char unique ID
    # Format appears timestamp-based + random
```

**Track Model:**
```python
{
    'tid': unique 20-char ID,
    'service': 'fbi_apostille' / 'embassy' / etc,
    'data': {
        'name': client name,
        'email': client email,
        'service': service key,
        'current_stage': stage code,
        'order_id': order ID,
        'order_type': order type,
        'shipping': shipping method (if applicable),
        'translation_r': translation required? (T/F),
        # + custom fields from Zoho webhooks
    },
    'created_at': timestamp,
    'updated_at': timestamp,
}
```

**Public Tracking Page:** `/tracking/<tid>/`
- GET endpoint (no auth required)
- Returns current stage + message
- Name redacted for privacy
- Shows timeline of stages
- Can be shared with client

**Stage Updates:**
- From Zoho webhook: `/tracking/crm/update/`
- Maps Zoho stage names to internal codes
- Triggers tracking email if stage changed

---

### 2. File Attachment System

**Model:** `FileAttachment` (GenericForeignKey)
```python
{
    'content_type': ForeignKey to ContentType,
    'object_id': ID of related object,
    'content_object': GenericForeignKey,
    'file': FileField (upload_to='orders/'),
    'uploaded_at': timestamp,
}
```

**Usage:**
```python
# Save attachments
save_file_attachments(request, FbiApostilleOrder, order)

# Builds URLs
file_urls = [
    request.build_absolute_uri(att.file.url)
    for att in order.file_attachments.all()
]

# Upload to Zoho
for file_url in file_urls:
    response = requests.get(file_url)
    zoho_client.upload_attachment(zoho_record_id,
                                   file_content=response.content,
                                   filename=...)
```

**Поддерживаемые модели:**
- FbiApostilleOrder
- MarriageOrder
- EmbassyLegalizationOrder
- ApostilleOrder
- TranslationOrder
- I9VerificationOrder

(QuoteRequest не имеет файлов)

---

### 3. Attribution Processing

**Extraction:**
```python
def extract_attribution_from_request(request):
    # From request.data['attribution'] or request.POST['attribution']
    # Parse if JSON string
    # Clean/normalize data
    # Remove nulls, empty strings
    # Convert numeric fields to int
    # Remove milliseconds from datetimes
    # Truncate long strings
```

**Normalization:**
```python
# Device type
'Mobile' → 'mobile'
'DESKTOP' → 'desktop'

# Source category
'google' → 'Google'
'yelp' → 'Yelp'
'direct' → 'Direct'

# Lead type
Default: 'form' for web forms
Override: 'phone' for WhatConverts leads

# Datetime
'2026-02-04T15:30:00.636Z' → '2026-02-04T15:30:00Z' (no ms)
```

**Zoho Payload Building:**
```python
def build_zoho_attribution_payload(attribution_data, lead_name):
    return {
        'Name': f"{lead_name[:20]} | {source}/{medium} | {datetime}",
        'Source': attribution_data['source'],
        'Source_Category': SOURCE_CATEGORIES[source],
        'Medium': attribution_data['medium'],
        'Campaign': attribution_data['campaign'],
        'Keyword': attribution_data['keyword'],
        'Landing_Page': attribution_data['landing_page'],
        'Lead_URL': attribution_data['lead_url'],
        'Referrer_Domain': attribution_data['referrer_domain'],
        'Device_Type': DEVICE_TYPE_OPTIONS[device_type],
        'Browser': attribution_data['browser'],
        'Pages_Viewed': int(attribution_data['pages_viewed']),
        'Visit_Count': int(attribution_data['visit_count']),
        'First_Visit_At': attribution_data['first_visit_at'],
        'City': attribution_data['city'],
        'State': attribution_data['state'],
        'Country': attribution_data['country'],
        'GCLID': attribution_data['gclid'],
        'FBCLID': attribution_data['fbclid'],
        'MSCLKID': attribution_data['msclkid'],
        'Lead_Type': 'Form',  # or 'Phone'
        'Call_Duration': attribution_data.get('call_duration'),
        'Call_Recording_URL': attribution_data.get('call_recording_url'),
        # Attribution_Record field set after record creation
    }
```

---

### 4. Stage Management

**Stage Definitions** (per service):

```python
STAGE_DEFINITIONS = {
    'fbi_apostille': [
        ('document_received', 'Order Received', 'Your documents have been received'),
        ('submitted', 'Submission in Progress', 'Your apostille is being processed'),
        ('processed_dos', 'Processing at U.S. DoS', 'Documents at Department of State'),
        ('translated', 'Translation Review', 'Translation being reviewed'),
        ('delivered', 'Out for Delivery', 'Your order is on the way'),
        ('completed', 'Completed', 'Order completed successfully'),
    ],
    'state_apostille': [
        ('document_received', 'Order Received', ...),
        ('quote_review', 'Request Under Review', ...),
        ('in_progress', 'In Progress', ...),
        ('delivered', 'Out for Delivery', ...),
        ('completed', 'Completed', ...),
    ],
    'embassy_legalization': [
        ('document_received', 'Order Received', ...),
        ('quote_review', 'Request Under Review', ...),
        ('notarized', 'Notarization Complete', ...),
        ('state_authenticated', 'State Authentication Complete', ...),
        ('federal_authenticated', 'Federal Authentication Complete', ...),
        ('embassy_legalized', 'Embassy Legalization Complete', ...),
        ('delivered', 'Out for Delivery', ...),
        ('completed', 'Completed', ...),
    ],
    # ... similar for translation, marriage, i9
}
```

**Stage Update Flow:**
```
1. Manager updates stage in Zoho CRM
   ↓
2. Zoho webhook → POST /tracking/crm/update/
   ├─ tid (tracking ID)
   ├─ stage (Zoho stage name)
   └─ service
   ↓
3. Django CrmUpdateStageView:
   ├─ Get Track by tid
   ├─ Map Zoho stage → internal code
   │  └─ "Order received" → "document_received"
   ├─ Update Track.data['current_stage']
   ├─ Track.save()
   └─ If stage actually changed:
      └─ send_tracking_email_task.delay(tid, stage_code)
   ↓
4. Email sent to client with update
```

---

## 🔒 Ключевые бизнес-правила

### 1. **TID (Tracking ID) Creation Timing**

| Order Type | TID Created | TID Written to Zoho |
|------------|-------------|---------------------|
| **Free** (Embassy, Apostille, Translation, I-9) | ✅ Сразу при создании order | ✅ Сразу при Zoho sync |
| **Paid** (FBI, Marriage) | ✅ При создании Stripe session (ДО оплаты) | ✅ ПОСЛЕ оплаты (в webhook) |

**Почему так:**
- Paid: TID нужен в Stripe metadata, но Zoho sync только после оплаты
- Free: Все происходит сразу, TID создается и пишется в Zoho одновременно

---

### 2. **Zoho Sync Timing**

```python
# Free orders
def create_embassy_order():
    order = EmbassyOrder.objects.create(...)
    process_new_order(order,
                     create_tid=True,      # ✅ Сразу
                     sync_zoho=True,       # ✅ Сразу
                     send_emails=True)     # ✅ Сразу

# Paid orders
def create_fbi_order():
    order = FbiOrder.objects.create(..., is_paid=False)
    process_attribution(request, order)   # ✅ Сразу
    save_file_attachments(request, order) # ✅ Сразу
    # NO TID, NO Zoho, NO emails yet! ❌

def create_stripe_session():
    create_tracking_record(order, 'fbi')  # ✅ TID создан
    # Still NO Zoho sync, NO emails ❌

def stripe_webhook_handler():
    order.is_paid = True
    sync_order_to_zoho_task.delay(...)    # ✅ NOW Zoho sync
    send_tracking_email_task.delay(...)   # ✅ NOW emails
```

---

### 3. **Phone Lead Matching Rules**

**Matching происходит только если:**
1. ✅ Номер телефона совпадает (последние 10 цифр)
2. ✅ Service type совпадает (FBI phone → FBI form only)
3. ✅ Phone lead создан раньше формы

**При matching:**
1. ✅ Phone lead обновляется данными формы (name, email)
2. ✅ WhatConverts attribution СОХРАНЯЕТСЯ (source, gclid, etc)
3. ✅ order.zoho_synced = True (предотвращает дубликат)
4. ✅ Zoho lead stage обновляется на "Order Received"
5. ✅ Контактные данные в Zoho обновляются

**НЕ matching если:**
1. ❌ Разные service types (FBI phone → Marriage form)
2. ❌ Разные номера
3. ❌ Phone lead не найден

---

### 4. **Attribution Priority**

```python
# Priority 1: Phone Lead (if matched)
if phone_lead:
    attribution = build_attribution_from_phone_lead(phone_lead)
    # Uses WhatConverts data: source, medium, gclid, etc
    attribution['lead_type'] = 'phone'

# Priority 2: Web Form (no phone lead)
else:
    attribution = extract_attribution_from_request(request)
    # Uses form data from JavaScript tracker
    attribution['lead_type'] = 'form'
```

**Результат:**
- Matched orders имеют `lead_type='phone'` и полные WhatConverts данные
- Обычные orders имеют `lead_type='form'` и веб-tracker данные

---

### 5. **Email Threading Rules**

**Staff Notifications:**
```python
Message-ID: <{service}-{date}@dcmn.us>
Thread: All orders of same service on same date group together
```

**Tracking Emails:**
```python
Message-ID: <tracking-{tid}@dcmn.us>
In-Reply-To: <tracking-{tid}@dcmn.us>
Thread: All updates for same order (by TID) group together
```

---

### 6. **Payment & Notification Flow**

```python
# Free orders - immediate
order created → TID created → Zoho sync → Staff email → Client email

# Paid orders - delayed until payment
order created → (wait for payment)
    ↓
Stripe session → TID created → (wait for payment)
    ↓
Payment received → Zoho sync → Staff email → Client email
```

**Manager notification sent только один раз:**
```python
if not order.manager_notified:
    send_staff_notification(...)
    order.manager_notified = True
```

---

### 7. **Duplicate Prevention**

**Phone Leads:**
- Check by `whatconverts_lead_id` (primary)
- Fallback: check by phone + email
- Update existing, don't create duplicate

**Orders:**
- No duplicate checking (allows multiple orders from same client)

**Phone Lead → Order Matching:**
- `order.zoho_synced = True` prevents Celery from creating duplicate Zoho lead
- Existing Zoho lead updated instead

---

### 8. **Service Detection Fallback**

```python
# Detected service → specific module
if detected_service:
    zoho_module = SERVICE_TO_ZOHO_MODULE[detected_service]
    # e.g., 'fbi' → 'FBI_Apostille'

# No service detected → Get a Quote
else:
    zoho_module = 'Get_a_Quote'
    logger.info("Service not detected, defaulting to Get_a_Quote")
```

**URL Patterns:**
- Specific service URLs → specific modules
- Homepage `/` → Get_a_Quote
- Unknown URLs → Get_a_Quote

---

### 9. **File Upload Constraints**

**Supported models:**
- ✅ FbiApostilleOrder
- ✅ MarriageOrder
- ✅ EmbassyLegalizationOrder
- ✅ ApostilleOrder
- ✅ TranslationOrder
- ✅ I9VerificationOrder
- ❌ QuoteRequest (no files)

**Upload flow:**
1. Save to Django filesystem (media/orders/)
2. Return absolute URLs in API response
3. Async upload to Zoho via Celery task
4. Link to Zoho record as attachments

---

### 10. **Stage Transition Rules**

**Initial stages:**
```python
'fbi_apostille': 'document_received'
'state_apostille': 'document_received'
'embassy': 'document_received'
'translation': 'document_received'
'marriage': 'document_received'
'i9': 'document_received'
```

**Phone Call specific:**
```python
'phone_call_received' → 'order_received' (on form submission)
```

**Updates:**
- Only from Zoho webhook (no direct update endpoint)
- Cannot skip stages (validation in CRM)
- Triggers email notification to client

---

## 📊 Data Flow Summary

```
SOURCES:
├─ Web Forms (7 types) → Orders
├─ WhatConverts → PhoneCallLeads
├─ Stripe → Payment confirmations
└─ Zoho Webhooks → Stage updates

PROCESSING:
├─ Attribution extraction/matching
├─ File attachment handling
├─ TID generation
├─ Phone lead matching
└─ Duplicate prevention

DESTINATIONS:
├─ Zoho CRM (primary)
│  ├─ Contacts
│  ├─ Leads/Deals (8 modules)
│  ├─ Lead Attribution Records
│  └─ File attachments
│
├─ Email (Resend API)
│  ├─ Staff notifications (instant)
│  ├─ Client welcome (after TID created)
│  └─ Status updates (on stage change)
│
└─ Tracking System (internal)
   └─ Public tracking page (/tracking/<tid>/)

ASYNC TASKS (Celery):
├─ sync_order_to_zoho_task
├─ send_tracking_email_task
├─ write_tracking_id_to_zoho_task
└─ send_staff_notification (synchronous currently)
```

---

## 🔧 Tech Stack

- **Framework:** Django 5.2 + Django REST Framework
- **Task Queue:** Celery (Zoho sync, emails)
- **Database:** PostgreSQL (production) / SQLite (dev)
- **Payment:** Stripe SDK
- **CRM:** Zoho CRM REST API
- **Email:** Resend API via Django EmailMessage
- **File Storage:** Django FileField (S3 or local media/)
- **Caching:** Django cache (Zoho tokens, 50 min TTL)

---

**Документация актуальна на:** 2026-02-09

**Последнее обновление:** WhatConverts integration + Phone lead matching
