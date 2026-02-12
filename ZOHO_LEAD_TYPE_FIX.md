# ✅ Исправление: Zoho Lead Type Picklist Values

## Проблема

Код использовал неправильные значения для Zoho Picklist `Lead_Type`.

### Было в коде:
```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',         # ❌ В Zoho: "Form Submission"
    'call': 'Call',         # ❌ В Zoho: "Phone Call"
    'phone': 'Call',        # ❌ В Zoho: "Phone Call"
    'chat': 'Chat',         # ✅ Правильно
}
```

### Реальные значения в Zoho:
1. ✅ **Phone Call**
2. ✅ **Form Submission**
3. ✅ **Chat**
4. ✅ **Email**
5. ✅ **Manual**

---

## ✅ Исправление

Обновили `LEAD_TYPE_OPTIONS` в `attribution.py`:

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form Submission',        # Web forms
    'call': 'Phone Call',             # Direct calls
    'phone': 'Phone Call',            # WhatConverts phone leads
    'chat': 'Chat',                   # Live chat
    'email': 'Email',                 # Email leads
    'manual': 'Manual',               # Manually created
}
```

**И обновили default:**
```python
# Было:
value = LEAD_TYPE_OPTIONS.get(str(value).lower(), 'Form')

# Стало:
value = LEAD_TYPE_OPTIONS.get(str(value).lower(), 'Form Submission')
```

---

## 📊 Mapping Django → Zoho

| Django lead_type | Zoho Lead_Type | Источник |
|-----------------|----------------|----------|
| `'form'` | `'Form Submission'` | Web forms на сайте |
| `'phone'` | `'Phone Call'` | WhatConverts phone calls |
| `'call'` | `'Phone Call'` | Direct calls (future) |
| `'chat'` | `'Chat'` | Live chat (future) |
| `'email'` | `'Email'` | Email leads (future) |
| `'manual'` | `'Manual'` | Manually created (future) |

---

## 🧪 Проверка

### Сценарий 1: Web Form
```
1. Клиент заполняет форму FBI на сайте
2. Django: lead_type = 'form'
3. Zoho: Lead_Type = 'Form Submission' ✅
```

### Сценарий 2: Phone Call
```
1. WhatConverts webhook (phone call)
2. Django: lead_type = 'phone'
3. Zoho: Lead_Type = 'Phone Call' ✅
```

### Сценарий 3: Phone → Form
```
1. Phone call → PhoneCallLead (lead_type='phone')
2. Form submission → Order created
3. Matching → Attribution from PhoneCallLead
4. Django: lead_type = 'phone'
5. Zoho: Lead_Type = 'Phone Call' ✅
```

---

## ✅ Результат

Теперь значения в Zoho **точно соответствуют** Picklist values:
- ✅ Web forms → **"Form Submission"**
- ✅ Phone calls → **"Phone Call"**
- ✅ Default → **"Form Submission"**

**Файл изменен:** `django_dcmn/orders/services/attribution.py` (строки 73-79, 212)
