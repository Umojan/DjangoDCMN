# WhatConverts Integration - Quick Reference

## ✅ Улучшения внесены

### 1. **Get a Quote как fallback**
- Если сервис не определен по URL → лид создается в **Get a Quote** модуле
- Менеджер квалифицирует и перемещает в нужный пайплайн

### 2. **Phone Lead Matching при заполнении формы**
- Поиск **только в том же пайплайне** (FBI phone lead → FBI form)
- Matching по номеру телефона (последние 10 цифр)
- **Сохраняет WhatConverts attribution** (source, medium, gclid)
- Обновляет только contact info (name, email)
- Zoho stage: "Phone Call Received" → "Order Received"

---

## 🔄 Flow

### Сценарий 1: Звонок → Форма (успешный matching)

```
10:00 AM - Клиент звонит
├─ WhatConverts webhook → Django
├─ PhoneCallLead создан (source="google", gclid="xyz123")
└─ Zoho: FBI_Apostille, Stage="Phone Call Received"

2:00 PM - Клиент заполняет форму FBI
├─ Django находит PhoneCallLead по phone + service
├─ Обновляет: name, email, matched_with_form=True
├─ СОХРАНЯЕТ: source="google", gclid="xyz123" (из phone lead!)
└─ Zoho: Stage="Order Received"
```

### Сценарий 2: Неопределенный источник

```
Клиент звонит с homepage (/)
├─ Сервис не определен
├─ PhoneCallLead.detected_service = ""
└─ Zoho: Get_a_Quote, Stage="Phone Call Received"
```

---

## 📋 Файлы

### Созданные файлы:
1. **models.py** - добавлена модель `PhoneCallLead`
2. **services/whatconverts.py** - обработка webhook, определение сервиса
3. **services/whatconverts_zoho.py** - синхронизация с Zoho
4. **services/phone_lead_matcher.py** - matching и обновление phone leads ← НОВЫЙ
5. **views/webhooks.py** - production webhook handler
6. **migrations/0026_phonecalllead.py** - миграция

### Обновленные файлы:
1. **services/attribution.py** - добавлена функция `check_and_update_phone_lead()` ← ОБНОВЛЕН
2. **services/whatconverts_zoho.py** - Get_a_Quote fallback ← ОБНОВЛЕН
3. **admin.py** - админка для PhoneCallLead
4. **urls.py** - новый endpoint `/api/orders/webhook/whatconverts/`

---

## 🎯 Ключевые функции

### `find_phone_lead_for_order(phone, service_type)` ← НОВАЯ
- Ищет phone lead только в том же пайплайне
- Нормализует номер телефона
- Возвращает последний созданный matching lead

### `update_phone_lead_with_form_data(phone_lead, order_data, ...)` ← НОВАЯ
- Обновляет contact info (name, email)
- **СОХРАНЯЕТ** WhatConverts attribution (source, medium, gclid)
- Помечает matched_with_form=True

### `update_zoho_lead_stage(phone_lead, "Order Received")` ← НОВАЯ
- Обновляет Zoho stage после matching

### `check_and_update_phone_lead(order, request)` ← НОВАЯ (в attribution.py)
- Вызывается в process_attribution() ПЕРВОЙ
- Если phone lead найден → использует его attribution
- Если нет → использует веб-форму attribution

---

## 🔍 Matching Rules

### ✅ Matched:
```python
Phone: fbi service, phone="555-1234"
Form:  FBI Order,   phone="555-1234"
→ MATCH ✅
```

### ❌ NOT Matched:
```python
Phone: fbi service,      phone="555-1234"
Form:  Marriage Order,   phone="555-1234"
→ NO MATCH ❌ (разные пайплайны)
```

```python
Phone: fbi service, phone="555-1234"
Form:  FBI Order,   phone="555-9999"
→ NO MATCH ❌ (разные номера)
```

---

## 📊 Attribution Priority

### 1. Phone Lead Found (Priority 1)
```json
{
  "source": "google",      // ← Из WhatConverts
  "medium": "cpc",         // ← Из WhatConverts
  "gclid": "xyz123",       // ← Из WhatConverts
  "lead_type": "phone"     // ← Остается phone!
}
```

### 2. No Phone Lead (Priority 2)
```json
{
  "source": "facebook",    // ← Из веб-формы tracker
  "medium": "social",      // ← Из веб-формы tracker
  "lead_type": "form"      // ← Обычная форма
}
```

---

## 🧪 Тестирование

### Тест 1: Phone → Form matching

```bash
# 1. Отправить phone webhook
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_type": "Phone Call",
    "lead_id": 999,
    "contact_phone_number": "+1-555-TEST-001",
    "landing_url": "https://dcmn.us/apostille-fbi",
    "lead_source": "google",
    "gclid": "test_gclid"
  }'

# 2. Заполнить FBI форму (тот же номер)
curl -X POST http://localhost:8000/api/orders/fbi/create-order/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test User",
    "phone": "+1-555-TEST-001",
    "email": "test@example.com",
    "package": 1
  }'

# Проверить:
# ✅ PhoneCallLead.matched_with_form = True
# ✅ order.attribution_data.gclid = "test_gclid"
# ✅ order.attribution_data.lead_type = "phone"
```

### Тест 2: Неопределенный источник (Get a Quote)

```bash
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
  -d '{
    "lead_type": "Phone Call",
    "landing_url": "https://dcmn.us/",
    "phone": "555-0000"
  }'

# Проверить:
# ✅ PhoneCallLead.detected_service = ""
# ✅ PhoneCallLead.zoho_module = "Get_a_Quote"
# ✅ Zoho: Lead создан в Get a Quote модуле
```

---

## 📚 Документация

- **WHATCONVERTS_INTEGRATION.md** - полная документация
- **WHATCONVERTS_SETUP.md** - quick start guide
- **PHONE_LEAD_MATCHING.md** - детальное описание matching flow ← НОВЫЙ
- **test_whatconverts_webhook.py** - тестовый скрипт

---

## 🚀 Deployment Checklist

- [ ] Применить миграцию: `python manage.py migrate orders`
- [ ] Добавить "Phone Call Received" stage во все модули Zoho
- [ ] Добавить "Order Received" stage во все модули Zoho
- [ ] Создать Get_a_Quote модуль в Zoho (если нет)
- [ ] Настроить webhook в WhatConverts
- [ ] Протестировать phone → form matching
- [ ] Протестировать Get a Quote fallback
- [ ] Проверить логи Django на ошибки
- [ ] Проверить attribution в orders.attribution_data

---

## 💡 Key Improvements

### До:
```
❌ Неопределенные источники → Deals (неправильно)
❌ Phone lead и form создавали дубликаты
❌ WhatConverts attribution терялась при matching
❌ Matching искал везде (не только в том же пайплайне)
```

### После:
```
✅ Неопределенные источники → Get_a_Quote
✅ Phone lead обновляется данными из формы (не дубликат)
✅ WhatConverts attribution СОХРАНЯЕТСЯ
✅ Matching только в том же пайплайне (FBI → FBI)
✅ Zoho stage автоматически обновляется
```

---

**Готово к production! 🎉**
