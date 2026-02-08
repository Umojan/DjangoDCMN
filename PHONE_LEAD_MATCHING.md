# Phone Lead Matching & Attribution Flow

## 🔄 Как работает matching телефонных лидов с веб-формами

### Сценарий: Клиент звонит → затем заполняет форму

```
1. Клиент звонит (понедельник 10:00)
   ├─ WhatConverts отправляет webhook
   ├─ Django создает PhoneCallLead
   │  ├─ contact_phone: +1-555-123-4567
   │  ├─ detected_service: "fbi"
   │  ├─ source: "google"
   │  ├─ medium: "cpc"
   │  ├─ campaign: "fbi apostille 2026"
   │  └─ gclid: "xyz123"
   └─ Zoho: создается Lead в FBI_Apostille
      └─ Stage: "Phone Call Received"

2. Менеджер говорит с клиентом
   └─ Просит заполнить форму на сайте

3. Клиент заполняет форму FBI Apostille (понедельник 14:00)
   ├─ Name: John Doe
   ├─ Phone: +1-555-123-4567  ← ТОТ ЖЕ НОМЕР
   ├─ Email: john@example.com
   └─ Django process_attribution() вызывается

4. Django находит matching phone lead
   ├─ Поиск по: phone + service_type ("fbi")
   ├─ Найден: PhoneCallLead #123 (создан 4 часа назад)
   └─ Действия:
      ├─ ✅ Обновляет PhoneCallLead:
      │  ├─ contact_name: "" → "John Doe"
      │  ├─ contact_email: "" → "john@example.com"
      │  ├─ matched_with_form: True
      │  ├─ matched_order_id: 456
      │  └─ СОХРАНЯЕТ WhatConverts attribution:
      │     ├─ source: "google" (не перезаписывается)
      │     ├─ medium: "cpc" (не перезаписывается)
      │     ├─ gclid: "xyz123" (не перезаписывается)
      │     └─ lead_type: "phone" (остается phone!)
      │
      ├─ ✅ Обновляет Zoho Lead:
      │  └─ Stage: "Phone Call Received" → "Order Received"
      │
      └─ ✅ Использует WhatConverts attribution для order.attribution_data
         └─ НЕ веб-форму attribution!
```

---

## 🎯 Ключевые правила matching

### 1. Поиск только в том же пайплайне

```python
# ✅ ПРАВИЛЬНО: Поиск FBI лида только среди FBI phone leads
phone_lead = PhoneCallLead.objects.filter(
    contact_phone__icontains=phone_last_10,
    detected_service='fbi'  # ← Только FBI!
).first()

# ❌ НЕПРАВИЛЬНО: Искать везде
phone_lead = PhoneCallLead.objects.filter(
    contact_phone__icontains=phone_last_10
    # Может найти Marriage lead вместо FBI!
).first()
```

### 2. Нормализация телефона

```python
# Входящий номер: "+1 (555) 123-4567"
# Нормализация: "5551234567"
# Matching: последние 10 цифр "5551234567"

# Все эти форматы будут matched:
# +1-555-123-4567
# (555) 123-4567
# 555.123.4567
# 15551234567
```

### 3. Сохранение WhatConverts атрибутов

**СОХРАНЯЮТСЯ (из phone lead):**
- ✅ source (google, yelp, etc.)
- ✅ medium (cpc, organic, etc.)
- ✅ campaign
- ✅ keyword
- ✅ gclid / msclkid
- ✅ lead_type = "phone"
- ✅ lead_score
- ✅ sentiment
- ✅ AI analysis

**ОБНОВЛЯЮТСЯ (из веб-формы):**
- 🔄 contact_name
- 🔄 contact_email
- 🔄 city / state / country (если есть)
- 🔄 matched_with_form = True
- 🔄 matched_order_id

---

## 📝 Пример: Полный flow

### Webhook от WhatConverts (10:00 AM)

```json
{
  "lead_id": 999001,
  "lead_type": "Phone Call",
  "contact_phone_number": "+1-555-123-4567",
  "landing_url": "https://dcmn.us/apostille-fbi",
  "lead_source": "google",
  "lead_medium": "cpc",
  "lead_campaign": "fbi apostille services",
  "gclid": "abc123xyz",
  "lead_score": 75
}
```

**Django создает:**
```python
PhoneCallLead(
    id=123,
    whatconverts_lead_id="999001",
    contact_phone="+1-555-123-4567",
    contact_name="",  # Пусто!
    contact_email="",  # Пусто!
    detected_service="fbi",
    source="google",
    medium="cpc",
    campaign="fbi apostille services",
    gclid="abc123xyz",
    lead_score=75,
    zoho_lead_id="5634000000123456",
    zoho_module="FBI_Apostille"
)
```

**Zoho создает:**
```
FBI Apostille Lead #5634000000123456
├─ Last Name: "Phone Lead"
├─ Phone: "+1-555-123-4567"
├─ Stage: "Phone Call Received"
└─ Lead Source: "Google"
```

---

### Веб-форма заполняется (2:00 PM)

```javascript
// Frontend отправляет
{
  "name": "John Doe",
  "email": "john@example.com",
  "phone": "+1-555-123-4567",  // ← ТОТ ЖЕ
  "package": "standard",
  // ... other fields
}
```

**Django process_attribution() вызывается:**

```python
# 1. check_and_update_phone_lead() выполняется первым
phone_lead = find_phone_lead_for_order(
    phone="+1-555-123-4567",
    service_type="fbi"
)

# 2. Найден PhoneCallLead #123
# 3. Обновляем его данными из формы:

phone_lead.contact_name = "John Doe"  # Было ""
phone_lead.contact_email = "john@example.com"  # Было ""
phone_lead.matched_with_form = True
phone_lead.matched_order_id = 456
phone_lead.save()

# 4. WhatConverts attribution СОХРАНЯЕТСЯ:
attribution = {
    'source': 'google',  # ← Из phone_lead, НЕ из формы!
    'medium': 'cpc',
    'campaign': 'fbi apostille services',
    'gclid': 'abc123xyz',
    'lead_type': 'phone',  # ← Остается 'phone'!
    'lead_score': 75,
}

# 5. Сохраняем в order.attribution_data
order.attribution_data = attribution
order.save()
```

**Zoho обновляется:**
```
FBI Apostille Lead #5634000000123456
├─ First Name: "John"
├─ Last Name: "Doe"
├─ Email: "john@example.com"
├─ Phone: "+1-555-123-4567"
├─ Stage: "Order Received"  ← ОБНОВЛЕНО!
└─ Lead Source: "Google"  ← НЕ изменилось!
```

---

## 🔍 Когда НЕ происходит matching

### Сценарий 1: Разные сервисы

```
Phone lead: service="fbi", phone="555-1234"
Web form: MarriageOrder, phone="555-1234"

Результат: ❌ НЕ matched (разные пайплайны)
```

### Сценарий 2: Разные номера

```
Phone lead: service="fbi", phone="555-1234"
Web form: FBI Order, phone="555-9999"

Результат: ❌ НЕ matched (разные номера)
```

### Сценарий 3: Нет номера в форме

```
Phone lead: service="fbi", phone="555-1234"
Web form: FBI Order, phone=""

Результат: ⏭️ Пропускается (нет номера для проверки)
```

### Сценарий 4: Форма заполнена первой (звонка не было)

```
Web form: FBI Order, phone="555-1234" (заполнена первой)

Результат: ✅ Обычный flow, attribution из формы
```

---

## 🎯 Get a Quote как fallback

### Неопределенный источник

```
WhatConverts webhook:
├─ landing_url: "https://dcmn.us/"  ← Homepage
└─ detected_service: None

Django:
├─ PhoneCallLead.detected_service = ""
└─ Zoho: создается в "Get_a_Quote" модуле
   └─ Stage: "Phone Call Received"

Менеджер:
└─ Квалифицирует и перемещает в нужный модуль
```

### URL patterns для определения сервиса

| Pattern | Service | Module |
|---------|---------|--------|
| `/apostille-fbi` | fbi | FBI_Apostille |
| `/triple-seal-marriage` | marriage | Marriage_Orders |
| `/embassy-legalization` | embassy | Embassy_Legalization |
| `/translation-services` | translation | Translation_Services |
| `/apostille` | apostille | Apostille_Orders |
| `/i-9` | i9 | I9_Verification |
| `/online-notary-form` | notary | Notary_Services |
| **Другие URL** | **None** | **Get_a_Quote** ← Fallback! |

---

## 📊 Attribution приоритет

```
Приоритет 1: Phone Lead (если matched)
├─ source: из WhatConverts
├─ medium: из WhatConverts
├─ campaign: из WhatConverts
├─ gclid: из WhatConverts
└─ lead_type: "phone"

Приоритет 2: Web Form (если phone lead не найден)
├─ source: из JavaScript tracker
├─ medium: из JavaScript tracker
├─ campaign: из JavaScript tracker
├─ gclid: из URL параметров
└─ lead_type: "form"
```

---

## 🧪 Тестирование matching

### Тест 1: Phone → Form (успешный matching)

```bash
# Шаг 1: Отправить phone webhook
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_type": "Phone Call",
    "lead_id": 888001,
    "contact_phone_number": "+1-555-777-8888",
    "landing_url": "https://dcmn.us/apostille-fbi",
    "lead_source": "google",
    "gclid": "test_gclid_123"
  }'

# Проверить: PhoneCallLead создан
# http://localhost:8000/admin/orders/phonecalllead/

# Шаг 2: Отправить FBI форму (тот же номер)
curl -X POST http://localhost:8000/api/orders/fbi/create-order/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test User",
    "email": "test@example.com",
    "phone": "+1-555-777-8888",
    "package": 1,
    "count": 1
  }'

# Проверить:
# 1. PhoneCallLead обновлен (contact_name, matched_with_form=True)
# 2. order.attribution_data содержит gclid="test_gclid_123"
# 3. Zoho lead stage = "Order Received"
```

### Тест 2: Разные сервисы (НЕ должен matched)

```bash
# Phone lead: FBI service
curl -X POST .../whatconverts/ \
  -d '{"landing_url": "https://dcmn.us/apostille-fbi", "phone": "555-1234"}'

# Form: Marriage (разный сервис)
curl -X POST .../marriage/create-order/ \
  -d '{"phone": "555-1234", ...}'

# Результат: PhoneCallLead НЕ обновлен (разные пайплайны)
```

---

## 📈 Отчеты и аналитика

### В Django Admin

**Phone Call Leads:**
```
Фильтры:
├─ Matched with form: Yes/No
├─ Service: FBI/Marriage/Embassy/etc.
├─ Zoho synced: Yes/No
└─ Created date

Показывает:
├─ Какие звонки конвертировались в заказы
├─ Какие источники лучше работают
└─ Сколько времени между звонком и заказом
```

### В Zoho

**Lead Attribution Records:**
```
Filter: Lead Type = "Phone"
├─ Все телефонные лиды
├─ Source breakdown (Google, Yelp, etc.)
└─ Linked to orders (через Attribution_Record lookup)
```

**Reports:**
```
"Phone Lead Conversion Rate"
├─ Total phone leads: 100
├─ Matched with orders: 45
└─ Conversion: 45%
```

---

## ✅ Итоговый чеклист

- [ ] PhoneCallLead модель создана
- [ ] Migration применена
- [ ] phone_lead_matcher.py создан
- [ ] attribution.py обновлен с check_and_update_phone_lead()
- [ ] Get_a_Quote как fallback настроен
- [ ] Тестирование matching успешно
- [ ] Zoho stage обновляется корректно
- [ ] WhatConverts attribution сохраняется при matching
- [ ] Matching работает только в том же пайплайне

---

**Готово! Система полностью интегрирована.** 🚀
