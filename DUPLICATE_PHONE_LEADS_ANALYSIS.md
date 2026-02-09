# 🔍 Анализ: Защита от дубликатов телефонных лидов

## Вопрос

**Что будет если человек позвонит 2 раза?**

---

## Текущая защита от дубликатов

### Уровень 1: По `whatconverts_lead_id` (строка 352-361)

```python
existing_lead = PhoneCallLead.objects.filter(
    whatconverts_lead_id=parsed['whatconverts_lead_id']
).first()

if existing_lead:
    # Обновляем существующий
    return existing_lead
```

**Когда срабатывает:**
- WhatConverts отправляет тот же webhook повторно (retry)
- `lead_id` одинаковый → обновляет существующий PhoneCallLead

**Проблема:** Если человек звонит **2 раза**, у WhatConverts будет **2 разных lead_id**
- Звонок 1: lead_id = 'WC-001'
- Звонок 2: lead_id = 'WC-002'

❌ **Не защищает от 2 звонков**

---

### Уровень 2: По phone/email через `find_duplicate_phone_lead()` (строка 364-375)

```python
duplicate = find_duplicate_phone_lead(
    phone=parsed['contact_phone'],
    email=parsed['contact_email']
)

if duplicate:
    # Обновляем дубликат
    return duplicate
```

**Функция `find_duplicate_phone_lead()` (строка 102):**

```python
def find_duplicate_phone_lead(phone: str = None, email: str = None):
    query = Q()

    if phone:
        normalized = ''.join(c for c in phone if c.isdigit())[-10:]
        query |= Q(contact_phone__icontains=normalized)

    if email:
        query |= Q(contact_email__iexact=email)

    return PhoneCallLead.objects.filter(query).order_by('-created_at').first()
```

**Проверка:** ✅ Ищет PhoneCallLead по phone

---

## 🧪 Симуляция: Человек звонит 2 раза (тот же сервис)

### Сценарий:
```
10:00 - Клиент звонит по поводу FBI Apostille
  → WhatConverts webhook: lead_id='WC-001', phone='+1 (555) 123-4567'

10:05 - Клиент звонит СНОВА по поводу FBI Apostille
  → WhatConverts webhook: lead_id='WC-002', phone='+1 (555) 123-4567'
```

### Обработка звонка #1 (10:00):

```
1. process_whatconverts_phone_lead(lead_id='WC-001')
2. detect_service → service='fbi'
3. find_matching_order(phone, service='fbi') → None (Order не существует)
4. PhoneCallLead.objects.filter(whatconverts_lead_id='WC-001') → None
5. find_duplicate_phone_lead(phone='+1 (555) 123-4567') → None
6. PhoneCallLead.objects.create(lead_id='WC-001', phone='+1 (555) 123-4567', service='fbi')
   → ✅ PhoneCallLead #100 создан
```

### Обработка звонка #2 (10:05):

```
1. process_whatconverts_phone_lead(lead_id='WC-002')
2. detect_service → service='fbi'
3. find_matching_order(phone, service='fbi') → None (Order все еще не существует)
4. PhoneCallLead.objects.filter(whatconverts_lead_id='WC-002') → None (другой lead_id)
5. find_duplicate_phone_lead(phone='+1 (555) 123-4567')
   → query = Q(contact_phone__icontains='5551234567')
   → PhoneCallLead.objects.filter(query).first()
   → ✅ НАХОДИТ PhoneCallLead #100

6. if duplicate:  ← ДА!
   → Update PhoneCallLead #100 with new data
   → return PhoneCallLead #100

7. ✅ PhoneCallLead #100 обновлен (НЕ создан новый)
```

### ✅ Результат: Дубликат НЕ создается

**НО ЕСТЬ ПРОБЛЕМА!** ⚠️

---

## ❌ ПРОБЛЕМА: Не учитывается service_type

### Проблемный сценарий:

```
10:00 - Клиент звонит по поводу FBI Apostille
  → PhoneCallLead #100: service='fbi', phone='+1 (555) 123-4567'
  → Zoho: FBI_Apostille (Phone Call Received)

10:05 - Клиент звонит по поводу I-9 Verification
  → WhatConverts webhook: lead_id='WC-002', phone='+1 (555) 123-4567', landing_url='/i-9-verification-form'
```

### Что происходит:

```
1. detect_service → service='i9'
2. find_matching_order(phone, service='i9') → None (I-9 Order не существует)
3. find_duplicate_phone_lead(phone='+1 (555) 123-4567')
   → query = Q(contact_phone__icontains='5551234567')
   → PhoneCallLead.objects.filter(query).first()
   → ✅ НАХОДИТ PhoneCallLead #100 (service='fbi')

4. if duplicate:  ← ДА!
   → Update PhoneCallLead #100
   → detected_service = 'i9'  ← ПЕРЕЗАПИСЫВАЕТ!
   → return PhoneCallLead #100

5. ❌ PhoneCallLead #100 обновлен:
   - Было: service='fbi'
   - Стало: service='i9'
   - Zoho lead в FBI_Apostille остался, но service изменился на i9
```

### ❌ Проблема:

1. PhoneCallLead для FBI был **перезаписан** на I-9
2. Zoho lead в FBI_Apostille остался, но данные изменились
3. Новый PhoneCallLead для I-9 **НЕ создался**
4. В Zoho I9_Verification лида **НЕТ**

---

## 🔧 Решение: Добавить service_type в find_duplicate_phone_lead()

### Текущий код (НЕПРАВИЛЬНО):

```python
def find_duplicate_phone_lead(phone: str = None, email: str = None):
    query = Q(contact_phone__icontains=normalized_phone[-10:])
    return PhoneCallLead.objects.filter(query).first()
```

**Проблема:** Ищет по ALL services

---

### Исправленный код (ПРАВИЛЬНО):

```python
def find_duplicate_phone_lead(phone: str = None, email: str = None, service_type: str = None):
    query = Q()

    if phone:
        normalized_phone = ''.join(c for c in phone if c.isdigit())
        if normalized_phone:
            query |= Q(contact_phone__icontains=normalized_phone[-10:])

    if email:
        query |= Q(contact_email__iexact=email)

    # КРИТИЧНО: Фильтровать по service_type
    if service_type:
        query &= Q(detected_service=service_type)

    if query:
        existing = PhoneCallLead.objects.filter(query).order_by('-created_at').first()
        if existing:
            logger.info(f"🔄 Found existing phone lead in '{service_type}' service: {existing.id}")
            return existing

    return None
```

---

### Обновить вызов в process_whatconverts_phone_lead():

```python
# Было:
duplicate = find_duplicate_phone_lead(
    phone=parsed['contact_phone'],
    email=parsed['contact_email']
)

# Должно быть:
duplicate = find_duplicate_phone_lead(
    phone=parsed['contact_phone'],
    email=parsed['contact_email'],
    service_type=parsed['detected_service']  # ← ДОБАВИТЬ
)
```

---

## 🧪 Симуляция с исправлением

### Сценарий: 2 звонка (разные сервисы)

```
10:00 - Звонок FBI
  → PhoneCallLead #100: service='fbi', phone='+1 (555) 123-4567'

10:05 - Звонок I-9
  → find_duplicate_phone_lead(phone='+1 (555) 123-4567', service_type='i9')
  → query = Q(contact_phone__icontains='5551234567') & Q(detected_service='i9')
  → PhoneCallLead.objects.filter(query).first()
  → ❌ НЕ НАХОДИТ (PhoneCallLead #100 имеет service='fbi', не 'i9')

  → PhoneCallLead.objects.create(service='i9', phone='+1 (555) 123-4567')
  → ✅ PhoneCallLead #101 создан (отдельный для I-9)
```

### ✅ Результат:
- PhoneCallLead #100 (FBI) - не изменен
- PhoneCallLead #101 (I-9) - создан новый
- В Zoho: FBI_Apostille + I9_Verification

---

### Сценарий: 2 звонка (тот же сервис)

```
10:00 - Звонок FBI
  → PhoneCallLead #100: service='fbi', phone='+1 (555) 123-4567'

10:05 - Звонок FBI снова
  → find_duplicate_phone_lead(phone='+1 (555) 123-4567', service_type='fbi')
  → query = Q(contact_phone__icontains='5551234567') & Q(detected_service='fbi')
  → ✅ НАХОДИТ PhoneCallLead #100

  → Update PhoneCallLead #100
  → ✅ Дубликат НЕ создан
```

### ✅ Результат:
- PhoneCallLead #100 (FBI) - обновлен
- НЕТ дубликатов

---

## 📊 Сравнение: До и После исправления

| Сценарий | Без service_type | С service_type |
|----------|-----------------|----------------|
| 2 звонка FBI | ✅ Обновляет #100 | ✅ Обновляет #100 |
| Звонок FBI + Звонок I-9 | ❌ Перезаписывает #100 (fbi→i9) | ✅ Создает #101 (i9) |
| 3 звонка FBI | ✅ Обновляет #100 3 раза | ✅ Обновляет #100 3 раза |
| Звонок FBI + Форма FBI | ✅ Обновляет #100 | ✅ Обновляет #100 |

---

## 🎯 Итоговое решение

### Нужно изменить 2 места:

1. **Функция `find_duplicate_phone_lead()`** (строка 102)
   - Добавить параметр `service_type`
   - Добавить фильтр `query &= Q(detected_service=service_type)`

2. **Вызов в `process_whatconverts_phone_lead()`** (строка 364)
   - Передать `service_type=parsed['detected_service']`

---

## ✅ После исправления

**Логика защиты от дубликатов:**

1. **Тот же webhook (lead_id)** → обновляет существующий
2. **Тот же phone + тот же service** → обновляет существующий
3. **Тот же phone + другой service** → создает новый PhoneCallLead

**Это правильная логика!** ✅

---

## 🚀 Требуется исправление

**Статус:** ❌ Критическая проблема найдена

**Решение:** Добавить `service_type` в `find_duplicate_phone_lead()`

**Приоритет:** Высокий (перед продакшеном)
