# ✅ Исправление: lead_type 'phone' → 'Call' в Zoho

## Проблема

Django использовал `lead_type = 'phone'` для WhatConverts phone calls, но этого значения не было в `LEAD_TYPE_OPTIONS`.

### Что происходило:

```python
# Django устанавливает:
attribution['lead_type'] = 'phone'  # WhatConverts phone call

# Zoho mapping:
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    # 'phone' НЕТ В СПИСКЕ!
}

# При синхронизации в Zoho:
value = LEAD_TYPE_OPTIONS.get('phone', 'Form')  # → 'Form' ❌
```

**Результат:** Phone calls записывались в Zoho как **'Form'** вместо **'Call'** ❌

---

## ✅ Исправление

Добавили alias `'phone': 'Call'` в `LEAD_TYPE_OPTIONS`:

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'phone': 'Call',  # ← ДОБАВИЛИ: WhatConverts phone leads → Zoho 'Call'
    'chat': 'Chat',
}
```

---

## 📊 До и После

### До исправления:
| Источник | lead_type в Django | lead_type в Zoho | Правильно? |
|----------|-------------------|------------------|-----------|
| Web Form | 'form' | 'Form' | ✅ |
| Phone Call | 'phone' | 'Form' ❌ | ❌ |

### После исправления:
| Источник | lead_type в Django | lead_type в Zoho | Правильно? |
|----------|-------------------|------------------|-----------|
| Web Form | 'form' | 'Form' | ✅ |
| Phone Call | 'phone' | **'Call'** ✅ | ✅ |

---

## 🧪 Проверка

### Сценарий: Phone call → Form

```
1. WhatConverts webhook (phone call)
   → PhoneCallLead создан
   → lead_type = 'phone'
   → Zoho: Lead_Type = 'Call' ✅

2. Клиент заполняет форму
   → Order создан
   → Matching с PhoneCallLead
   → Attribution из WhatConverts: lead_type = 'phone'
   → Zoho Attribution Record: Lead_Type = 'Call' ✅
```

✅ **Правильно!**

---

## 📋 Все возможные lead_type в системе

| Значение в Django | Значение в Zoho | Источник |
|------------------|----------------|----------|
| `'form'` | `'Form'` | Web forms (любые формы на сайте) |
| `'call'` | `'Call'` | Direct call (если добавите в будущем) |
| `'phone'` | `'Call'` | WhatConverts phone calls ✅ |
| `'chat'` | `'Chat'` | Live chat (зарезервировано) |

---

## ✅ Готово

Теперь WhatConverts phone calls правильно записываются в Zoho как **'Call'** вместо 'Form'.

**Файл изменен:** `django_dcmn/orders/services/attribution.py` (строка 75)
