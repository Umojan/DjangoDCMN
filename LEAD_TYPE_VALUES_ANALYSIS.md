# 📋 Lead Type в Django Attribution Records

## Какие значения lead_type Django создает

### В коде определены 3 значения (attribution.py строка 73):

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'chat': 'Chat',
}
```

---

## 🔍 Что Django записывает в lead_type

### 1. **'phone'** (Phone Lead из WhatConverts)

**Код (whatconverts.py:422):**
```python
'lead_type': 'phone',  # Always 'phone' for WhatConverts
```

**Когда:**
- Phone call → PhoneCallLead создан
- Клиент заполняет форму → Matching с PhoneCallLead
- Attribution берется из WhatConverts

**Проблема:** ❌ **'phone' НЕ в списке LEAD_TYPE_OPTIONS!**

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',   # ← Должно быть 'call', а не 'phone'!
    'chat': 'Chat',
}
```

---

### 2. **'form'** (Web Form)

**Код (attribution.py:327):**
```python
if 'lead_type' not in attribution or not attribution['lead_type']:
    attribution['lead_type'] = 'form'
```

**Когда:**
- Клиент заполняет форму на сайте
- PhoneCallLead НЕ существует
- Regular web form attribution

✅ **'form' есть в LEAD_TYPE_OPTIONS**

---

## ❌ ПРОБЛЕМА: 'phone' vs 'call'

### В коде используется 'phone':
```python
# whatconverts.py:422
'lead_type': 'phone',  # ← НЕТ В LEAD_TYPE_OPTIONS
```

### Но в Zoho Picklist есть 'call':
```python
# attribution.py:73
LEAD_TYPE_OPTIONS = {
    'call': 'Call',  # ← В Zoho должно быть 'Call'
}
```

### Что происходит при синхронизации в Zoho:

```python
# attribution.py:209
value = LEAD_TYPE_OPTIONS.get(str(value).lower(), 'Form')
```

**Сценарий:**
```
1. lead_type = 'phone' (из WhatConverts)
2. LEAD_TYPE_OPTIONS.get('phone', 'Form')
3. 'phone' НЕ найдено в словаре
4. Возвращает default = 'Form'
5. ❌ В Zoho записывается 'Form' вместо 'Call'!
```

---

## ✅ РЕШЕНИЕ

### Вариант 1: Добавить 'phone' в LEAD_TYPE_OPTIONS

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'phone': 'Call',  # ← ДОБАВИТЬ (alias для 'call')
    'chat': 'Chat',
}
```

**Плюсы:**
- Минимальные изменения
- 'phone' → 'Call' в Zoho

---

### Вариант 2: Изменить 'phone' на 'call' в коде

```python
# whatconverts.py:422
# Было:
'lead_type': 'phone',

# Стало:
'lead_type': 'call',
```

**Плюсы:**
- Соответствует Zoho Picklist
- Не нужно добавлять alias

**Минусы:**
- Нужно изменить несколько мест в коде

---

## 📊 Текущая ситуация

| Источник | lead_type в Django | lead_type в Zoho | Правильно? |
|----------|-------------------|------------------|-----------|
| Web Form | 'form' | 'Form' | ✅ Да |
| Phone Call (WhatConverts) | 'phone' | 'Form' ❌ | ❌ НЕТ! |

**Проблема:** Phone calls записываются в Zoho как 'Form' вместо 'Call'

---

## ✅ Рекомендуемое решение

**Использовать Вариант 1** (добавить alias):

```python
LEAD_TYPE_OPTIONS = {
    'form': 'Form',
    'call': 'Call',
    'phone': 'Call',  # Alias: WhatConverts phone leads
    'chat': 'Chat',
}
```

**Почему:**
- Минимальные изменения (1 строка)
- Сохраняет семантику ('phone' понятнее чем 'call' в Django коде)
- Правильно мапится в Zoho ('Call')

---

## 🎯 Итог

**Текущие lead_type в Django:**
1. ✅ `'form'` - Web forms
2. ❌ `'phone'` - Phone calls (НЕ мапится в Zoho правильно)
3. ❓ `'chat'` - Нет в коде (зарезервировано для будущего)

**Нужно исправить:** Добавить `'phone': 'Call'` в `LEAD_TYPE_OPTIONS`
