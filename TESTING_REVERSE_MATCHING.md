# 🧪 Тестирование реверсивного матчинга (Форма → Звонок)

## Что нужно протестировать

Проверить что система правильно работает в обоих направлениях:

1. ✅ **Звонок → Форма** (было раньше)
2. ✅ **Форма → Звонок** (НОВОЕ)

## Тестовые сценарии

### Сценарий 1: Форма → Звонок (уточняющий звонок)

**Ожидаемое поведение:** PhoneCallLead НЕ создается

#### Шаги:

1. **Заполнить форму FBI Apostille:**
   ```
   Name: Test User
   Phone: +1 (555) 123-4567
   Email: test@example.com
   ```

2. **Проверить что Order создан:**
   ```bash
   # Django admin
   Orders → FBI Apostille Orders
   # Должен быть новый order с phone=+1 (555) 123-4567
   ```

3. **Отправить тестовый WhatConverts webhook:**
   ```bash
   curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
     -H "Content-Type: application/json" \
     -d '{
       "lead_id": "TEST-001",
       "lead_type": "Phone Call",
       "contact_name": "Test User",
       "contact_phone_number": "+1 (555) 123-4567",
       "contact_email_address": "test@example.com",
       "landing_url": "https://dcmn.com/apostille-fbi-form",
       "lead_source": "google",
       "lead_medium": "cpc",
       "date_created": "2026-02-09T10:00:00Z"
     }'
   ```

4. **Проверить логи Django:**
   ```
   ================================================================================
   📞 Processing WhatConverts Phone Lead: TEST-001
      Contact: Test User | +1 (555) 123-4567
      Service: fbi
      Landing: https://dcmn.com/apostille-fbi-form
   ================================================================================
   🔍 Searching for orders in 'fbi' pipeline only
   ✅ Found matching fbi order: [ID]
   ================================================================================
   ⏭️ SKIPPING PHONE LEAD CREATION
      Found existing fbi order #[ID]
      Contact: Test User | +1 (555) 123-4567
      90% probability: Clarification call about existing order
      If customer wants NEW service, they'll fill out a form
   ================================================================================
   ```

5. **Проверить ответ webhook:**
   ```json
   {
     "status": "skipped",
     "reason": "Matching order already exists",
     "message": "90% probability: clarification call about existing order"
   }
   ```

6. **Проверить что PhoneCallLead НЕ создан:**
   ```bash
   # Django admin
   Orders → Phone Call Leads
   # НЕ должно быть лида с lead_id=TEST-001
   ```

✅ **Ожидается:** PhoneCallLead НЕ создается, webhook возвращает "skipped"

---

### Сценарий 2: Форма FBI → Звонок I-9 (новая услуга)

**Ожидаемое поведение:** PhoneCallLead создается (разные сервисы)

#### Шаги:

1. **Заполнить форму FBI Apostille:**
   ```
   Phone: +1 (555) 999-8888
   Email: different@example.com
   ```

2. **Отправить webhook для I-9:**
   ```bash
   curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
     -H "Content-Type: application/json" \
     -d '{
       "lead_id": "TEST-002",
       "lead_type": "Phone Call",
       "contact_phone_number": "+1 (555) 999-8888",
       "landing_url": "https://dcmn.com/i-9-verification-form",
       "lead_source": "google"
     }'
   ```

3. **Проверить логи:**
   ```
   🔍 Searching for orders in 'i9' pipeline only
   ✓ No existing order found, proceeding with phone lead creation
   ✅ Created new phone lead: [ID]
   ```

4. **Проверить что PhoneCallLead создан:**
   ```bash
   # Django admin
   Orders → Phone Call Leads
   # Должен быть лид с:
   # - lead_id = TEST-002
   # - detected_service = i9
   # - phone = +1 (555) 999-8888
   ```

5. **Проверить Zoho:**
   ```
   Zoho → I9_Verification
   # Должен быть новый лид с Stage = "Phone Call Received"
   ```

✅ **Ожидается:** PhoneCallLead создается, синкается в Zoho I9_Verification

---

### Сценарий 3: Звонок → Форма (классический flow)

**Ожидаемое поведение:** PhoneCallLead создается, затем обновляется формой

#### Шаги:

1. **Отправить webhook (звонок):**
   ```bash
   curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
     -H "Content-Type: application/json" \
     -d '{
       "lead_id": "TEST-003",
       "lead_type": "Phone Call",
       "contact_name": "Jane Doe",
       "contact_phone_number": "+1 (555) 777-6666",
       "contact_email_address": "jane@example.com",
       "landing_url": "https://dcmn.com/seal-marriage-form",
       "lead_source": "facebook",
       "lead_campaign": "summer-promo",
       "gclid": "abc123xyz"
     }'
   ```

2. **Проверить что PhoneCallLead создан:**
   ```bash
   # Django admin
   Orders → Phone Call Leads
   # Должен быть лид TEST-003 с detected_service=marriage
   ```

3. **Проверить Zoho:**
   ```
   Zoho → Marriage_Orders
   # Лид должен быть в колонке "Phone Call Received"
   ```

4. **Заполнить форму Marriage Order:**
   ```
   Name: Jane Doe
   Phone: +1 (555) 777-6666
   Email: jane@example.com
   ```

5. **Проверить логи Django:**
   ```
   🔍 Searching for phone lead: phone=5557776666, service=marriage
   ✅ Found matching phone lead: [ID] (created [timestamp])
   🔄 Updating phone lead [ID] with form data
   ✅ Marked order [ID] as synced (linked to Zoho lead [ID])
      This prevents Celery task from creating duplicate lead
   📤 Updating Zoho lead [ID] in Marriage_Orders
      New stage: Order Received
   ✅ Updated Zoho lead with form data and stage 'Order Received'
   ```

6. **Проверить Zoho:**
   ```
   Zoho → Marriage_Orders
   # Лид должен переместиться в "Order Received"
   # Email должен обновиться на jane@example.com
   ```

7. **Проверить что НЕТ дубликата:**
   ```bash
   # Проверить что в Zoho только 1 лид для jane@example.com
   # Проверить что Order.zoho_synced = True
   ```

✅ **Ожидается:** PhoneCallLead обновляется, переносится в "Order Received", нет дубликатов

---

### Сценарий 4: Игнорирование tracking страниц

**Ожидаемое поведение:** Webhook игнорируется

#### Шаги:

1. **Отправить webhook с /tracking URL:**
   ```bash
   curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
     -H "Content-Type: application/json" \
     -d '{
       "lead_id": "TEST-004",
       "lead_type": "Phone Call",
       "landing_url": "https://dcmn.com/tracking/order-status"
     }'
   ```

2. **Проверить ответ:**
   ```json
   {
     "status": "skipped",
     "reason": "Tracking page lead ignored"
   }
   ```

✅ **Ожидается:** Webhook возвращает "skipped"

---

### Сценарий 5: Неизвестный сервис → Get_a_Quote

**Ожидаемое поведение:** PhoneCallLead создается в Get_a_Quote

#### Шаги:

1. **Отправить webhook с неизвестным URL:**
   ```bash
   curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
     -H "Content-Type: application/json" \
     -d '{
       "lead_id": "TEST-005",
       "lead_type": "Phone Call",
       "contact_phone_number": "+1 (555) 444-3333",
       "landing_url": "https://dcmn.com/some-random-page"
     }'
   ```

2. **Проверить логи:**
   ```
   ❓ Could not detect service from URL: https://dcmn.com/some-random-page
   ✅ Created new phone lead: [ID]
   Service not detected, defaulting to Get_a_Quote
   ```

3. **Проверить Zoho:**
   ```
   Zoho → Get_a_Quote
   # Должен быть новый лид с Stage = "Phone Call Received"
   ```

✅ **Ожидается:** PhoneCallLead создается, синкается в Get_a_Quote

---

## Чеклист для тестирования

### Перед тестированием:

- [ ] Применить миграции: `python manage.py migrate`
- [ ] Добавить стадии в Zoho:
  - "Phone Call Received" (для всех модулей)
  - "Order Received" (для всех модулей)
- [ ] Убедиться что Zoho API токены валидны
- [ ] Запустить Django сервер: `python manage.py runserver`

### Тесты:

- [ ] ✅ Сценарий 1: Форма → Звонок (тот же сервис) → PhoneCallLead НЕ создается
- [ ] ✅ Сценарий 2: Форма FBI → Звонок I-9 → PhoneCallLead создается
- [ ] ✅ Сценарий 3: Звонок → Форма → PhoneCallLead обновляется
- [ ] ✅ Сценарий 4: /tracking URL → игнорируется
- [ ] ✅ Сценарий 5: Неизвестный URL → Get_a_Quote

### После тестирования:

- [ ] Проверить отсутствие дубликатов в Zoho
- [ ] Проверить что attribution данные сохранены (source, gclid, etc.)
- [ ] Проверить логи на ошибки
- [ ] Проверить что Celery таски не создают дубли

---

## Продакшн webhook URL

После успешного тестирования настроить в WhatConverts:

```
Production URL: https://your-domain.com/api/orders/webhook/whatconverts/
Method: POST
Content-Type: application/json
```

Фильтры в WhatConverts:
- ✅ Отправлять только "Phone Call" leads
- ✅ Не отправлять spam leads
- ✅ Отправлять сразу после звонка (real-time)

---

## Troubleshooting

### PhoneCallLead создается, хотя Order существует

**Проблема:** Не находит matching order

**Решение:**
1. Проверить что телефоны совпадают (последние 10 цифр)
2. Проверить что сервис совпадает (FBI → FBI, не FBI → I-9)
3. Проверить логи:
   ```
   🔍 Searching for orders in 'fbi' pipeline only
   ```

### Zoho duplicate leads создаются

**Проблема:** `order.zoho_synced` не установлен

**Решение:**
1. Проверить что в `phone_lead_matcher.py` установлен флаг:
   ```python
   order_instance.zoho_synced = True
   order_instance.save(update_fields=['zoho_synced'])
   ```

### Webhook возвращает 500 error

**Проблема:** Ошибка в коде

**Решение:**
1. Проверить логи Django:
   ```bash
   tail -f logs/django.log
   ```
2. Проверить что все импорты на месте
3. Проверить что миграции применены

---

## Итог

После прохождения всех тестов система должна:

✅ Создавать PhoneCallLead только когда Order НЕ существует
✅ Пропускать PhoneCallLead если Order уже есть (90% вероятность: уточняющий звонок)
✅ Матчить только в рамках одного сервиса (FBI → FBI, не FBI → I-9)
✅ Обновлять существующие PhoneCallLead при заполнении формы
✅ Не создавать дубликаты в Zoho
✅ Сохранять WhatConverts attribution данные
✅ Игнорировать tracking страницы
✅ Использовать Get_a_Quote для неизвестных сервисов
