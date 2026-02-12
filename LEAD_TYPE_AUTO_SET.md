# ✅ Автоматическая установка Lead Type = "Form"

## Изменения

Теперь Django автоматически устанавливает `lead_type = 'form'` для всех веб-форм, если значение не передано с фронтенда.

## Что было исправлено

### 1. В `process_attribution()` (строка ~306)

**Добавлено:**
```python
# Set default lead_type to 'Form' if not provided
# (All web forms are 'Form' type, unless explicitly overridden)
if 'lead_type' not in attribution or not attribution['lead_type']:
    attribution['lead_type'] = 'form'
```

Это гарантирует что в `order.attribution_data` всегда будет:
```json
{
  "source": "google",
  "medium": "cpc",
  "lead_type": "form"  // ← Автоматически установлено
}
```

### 2. В `build_zoho_attribution_payload()` (строка ~227)

**Добавлено:**
```python
# Ensure Lead_Type is always set (default to 'Form' for web submissions)
if 'Lead_Type' not in payload:
    payload['Lead_Type'] = 'Form'
```

Это гарантирует что Zoho payload всегда содержит:
```json
{
  "Name": "John Doe | google/cpc | 2026-02-03 12:00",
  "Source": "google",
  "Lead_Type": "Form"  // ← Всегда установлено
}
```

## Приоритет значений

1. **Frontend явно указал** `lead_type`:
   ```javascript
   attribution: { lead_type: 'call' }
   ```
   ✅ Используется `'call'`

2. **Frontend не передал** `lead_type`:
   ```javascript
   attribution: { source: 'google' }
   ```
   ✅ Django устанавливает `'form'`

3. **Frontend передал пустое значение**:
   ```javascript
   attribution: { lead_type: null }
   ```
   ✅ Django устанавливает `'form'`

## Zoho Picklist значения

Zoho CRM принимает (case-sensitive):
- `Form` ✅
- `Call` ✅
- `Chat` ✅

Django нормализует:
```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'chat': 'Chat',
}

# 'form' → 'Form'
# 'FORM' → 'Form'
# 'Form' → 'Form'
```

## Как это работает для разных источников

### 1. Веб-формы (все)
```python
# FBI, Marriage, Quote, Embassy, Translation, Apostille, I-9
attribution_data = {
    'source': 'google',
    'lead_type': 'form'  # ← Автоматически
}
```
→ Zoho: `Lead_Type = "Form"` ✅

### 2. WhatConverts (звонки)
Если в будущем подключите WhatConverts для звонков:
```python
attribution_data = {
    'source': 'google',
    'lead_type': 'call',  # ← Явно от WhatConverts
    'call_duration': 120,
    'call_recording_url': 'https://...'
}
```
→ Zoho: `Lead_Type = "Call"` ✅

### 3. Live Chat
Если подключите чат:
```python
attribution_data = {
    'source': 'website',
    'lead_type': 'chat'  # ← Явно от чата
}
```
→ Zoho: `Lead_Type = "Chat"` ✅

## Проверка в Zoho

После создания лида через любую форму:

1. Откройте `Lead_Attribution_Records`
2. Найдите запись
3. Поле **Lead Type** должно быть = **"Form Submission"** (или как отображается в вашем Zoho)

## Frontend (опционально)

Можно явно отправлять с фронтенда, но это **не обязательно**:

```javascript
// Опционально - можно добавить
if (window.DCMNTracker) {
    const attr = window.DCMNTracker.getAttribution();
    attr.lead_type = 'form';  // Явно указываем
    payload.attribution = attr;
}
```

Но даже без этого Django автоматически установит `'form'`.

## Итог

✅ **Все веб-формы автоматически получают `lead_type = 'form'`**

✅ **Zoho всегда показывает правильный Lead Type**

✅ **Не нужно менять frontend код**

✅ **Поддержка будущих источников (call, chat) готова**
