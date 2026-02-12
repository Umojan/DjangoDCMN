# ✅ Deployment Checklist: Реверсивный матчинг

## Перед деплоем

### 1. Код готов
- [x] `services/whatconverts.py` - обновлена функция `find_matching_order()` с параметром `service_type`
- [x] `services/whatconverts.py` - обновлена функция `process_whatconverts_phone_lead()` с проверкой Order ПЕРЕД созданием PhoneCallLead
- [x] `views/webhooks.py` - обновлен обработчик `None` возврата
- [x] `services/phone_lead_matcher.py` - устанавливает `order.zoho_synced = True`
- [x] `services/attribution.py` - добавлен импорт `Optional, Dict`

### 2. Документация создана
- [x] `REVERSE_MATCHING_IMPLEMENTED.md` - описание реализации
- [x] `TESTING_REVERSE_MATCHING.md` - тестовые сценарии
- [x] `IMPLEMENTATION_SUMMARY.md` - итоговый summary
- [x] `BIDIRECTIONAL_MATCHING_FLOW.md` - визуальные схемы
- [x] `DEPLOYMENT_CHECKLIST.md` - этот файл

---

## Deployment Steps

### Step 1: Локальное тестирование

```bash
# 1. Запустить локальный сервер
python manage.py runserver

# 2. Протестировать Форма → Звонок (тот же сервис)
# Ожидается: PhoneCallLead НЕ создается
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_id": "TEST-001",
    "lead_type": "Phone Call",
    "contact_phone_number": "+1 (555) 123-4567",
    "landing_url": "https://dcmn.com/apostille-fbi-form"
  }'

# 3. Проверить логи
tail -f logs/django.log
# Должно быть: "⏭️ SKIPPING PHONE LEAD CREATION"

# 4. Проверить webhook response
# Должно быть: {"status": "skipped", "reason": "Matching order already exists"}
```

**Checklist:**
- [ ] Webhook возвращает `"status": "skipped"` когда Order существует
- [ ] PhoneCallLead НЕ создается в Django admin
- [ ] Логи показывают "⏭️ SKIPPING PHONE LEAD CREATION"

---

### Step 2: Тестирование Phone → Form (классический flow)

```bash
# 1. Отправить webhook (звонок)
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_id": "TEST-002",
    "lead_type": "Phone Call",
    "contact_phone_number": "+1 (555) 999-8888",
    "landing_url": "https://dcmn.com/seal-marriage-form",
    "lead_source": "google",
    "gclid": "abc123"
  }'

# 2. Проверить что PhoneCallLead создан
# Django admin → Phone Call Leads → должен быть TEST-002

# 3. Заполнить форму Marriage Order с тем же телефоном
# Проверить логи

# 4. Проверить что НЕТ дубликата в Zoho
```

**Checklist:**
- [ ] PhoneCallLead создается при первом звонке
- [ ] PhoneCallLead обновляется при заполнении формы
- [ ] `order.zoho_synced = True` установлен
- [ ] Zoho Stage меняется на "Order Received"
- [ ] НЕТ дубликатов в Zoho

---

### Step 3: Git commit

```bash
# 1. Проверить изменения
git status

# 2. Добавить файлы
git add django_dcmn/orders/services/whatconverts.py
git add django_dcmn/orders/views/webhooks.py
git add REVERSE_MATCHING_IMPLEMENTED.md
git add TESTING_REVERSE_MATCHING.md
git add IMPLEMENTATION_SUMMARY.md
git add BIDIRECTIONAL_MATCHING_FLOW.md
git add DEPLOYMENT_CHECKLIST.md

# 3. Создать commit
git commit -m "Implement bidirectional matching: Form → Phone call detection

- Form exists → Phone call arrives → Skip PhoneCallLead creation (90% clarification call)
- Added service_type parameter to find_matching_order() for same-service matching
- Updated process_whatconverts_phone_lead() to check orders BEFORE creating phone lead
- Updated webhook handler to handle None return (skipped phone lead)
- Phone → Form matching still works (PhoneCallLead updates on form submission)
- Prevents duplicate leads in Zoho
- Preserves WhatConverts attribution data

Files changed:
- services/whatconverts.py: Added service_type filtering, check orders first
- views/webhooks.py: Handle None return for skipped phone leads
- services/phone_lead_matcher.py: Sets order.zoho_synced=True (already existed)

Documentation:
- REVERSE_MATCHING_IMPLEMENTED.md
- TESTING_REVERSE_MATCHING.md
- IMPLEMENTATION_SUMMARY.md
- BIDIRECTIONAL_MATCHING_FLOW.md
- DEPLOYMENT_CHECKLIST.md

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"

# 4. Push to remote
git push origin main
```

**Checklist:**
- [ ] Все изменения закоммичены
- [ ] Commit message понятный
- [ ] Pushed to remote

---

### Step 4: Deployment

```bash
# Railway/Heroku/your-deployment-platform
# После push автоматически деплоится

# Проверить логи деплоя
railway logs
# или
heroku logs --tail
```

**Checklist:**
- [ ] Deployment успешный
- [ ] Нет ошибок в логах
- [ ] Django сервер запущен

---

### Step 5: Продакшн тестирование

```bash
# 1. Протестировать production webhook
curl -X POST https://your-domain.com/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_id": "PROD-TEST-001",
    "lead_type": "Phone Call",
    "contact_phone_number": "+1 (555) 111-2222",
    "landing_url": "https://dcmn.com/apostille-fbi-form"
  }'

# 2. Проверить логи production
railway logs
# или
heroku logs --tail --source app

# 3. Проверить Django admin (production)
# https://your-domain.com/admin

# 4. Проверить Zoho CRM
```

**Checklist:**
- [ ] Production webhook работает
- [ ] Логи показывают правильное поведение
- [ ] PhoneCallLead создается/пропускается корректно
- [ ] Нет ошибок в Zoho sync

---

### Step 6: Настройка WhatConverts

```
1. Залогиниться в WhatConverts dashboard
2. Settings → Webhooks
3. Добавить новый webhook:
   - URL: https://your-domain.com/api/orders/webhook/whatconverts/
   - Method: POST
   - Content-Type: application/json
   - Events: New Lead (Phone Call only)
   - Active: Yes

4. Test webhook:
   - Send test phone call lead
   - Проверить что приходит в Django
   - Проверить что создается PhoneCallLead
   - Проверить что синкается в Zoho
```

**Checklist:**
- [ ] WhatConverts webhook настроен
- [ ] URL правильный (https)
- [ ] Только "Phone Call" leads включены
- [ ] Test webhook успешный
- [ ] Real-time delivery включен

---

### Step 7: Zoho CRM настройка

```
Для КАЖДОГО модуля добавить стадии:
- FBI_Apostille
- Marriage_Orders
- Embassy_Legalization
- I9_Verification
- Translation_Services
- Apostille_Orders
- Notary_Services
- Get_a_Quote

Стадии (в порядке):
1. Phone Call Received  ← НОВАЯ
2. Order Received       ← СУЩЕСТВУЮЩАЯ
3. Processing
4. Completed
5. Cancelled
```

**Checklist:**
- [ ] "Phone Call Received" добавлена во все модули
- [ ] "Order Received" существует во всех модулях
- [ ] Порядок стадий правильный
- [ ] Lead Attribution Records работают

---

## Monitoring

### Логи для мониторинга

#### ✅ Успешный skip (Form → Phone, тот же сервис):
```
⏭️ SKIPPING PHONE LEAD CREATION
   Found existing fbi order #123
   90% probability: Clarification call about existing order
```

#### ✅ Успешное создание (Phone первый):
```
✅ Created new phone lead: 456
📤 Syncing phone lead to Zoho...
✅ Successfully synced to Zoho: FBI_Apostille
```

#### ✅ Успешное обновление (Phone → Form):
```
🔍 Searching for phone lead: phone=5551234567, service=fbi
✅ Found matching phone lead: 456
🔄 Updating phone lead 456 with form data
✅ Marked order 789 as synced
📤 Updating Zoho lead stage to 'Order Received'
```

#### ❌ Ошибки для расследования:
```
❌ Failed to process phone lead
❌ Failed to update Zoho lead
❌ Error updating phone lead
```

---

## Rollback Plan

Если что-то пошло не так:

### Опция 1: Git revert

```bash
# Вернуться к предыдущему коммиту
git log --oneline  # найти hash последнего good commit
git revert <commit-hash>
git push origin main
```

### Опция 2: Отключить WhatConverts webhook

```
1. WhatConverts dashboard → Webhooks
2. Найти webhook
3. Active: No (выключить)
4. Исправить проблему
5. Active: Yes (включить снова)
```

### Опция 3: Временный фикс в коде

```python
# В process_whatconverts_phone_lead()
# Временно закомментировать проверку Order

# match = find_matching_order(...)
# if match:
#     return None

# Это вернет старое поведение (всегда создавать PhoneCallLead)
```

---

## Success Criteria

### ✅ Deployment успешен если:

1. **Форма → Звонок (тот же сервис):**
   - PhoneCallLead НЕ создается
   - Webhook возвращает `"status": "skipped"`
   - Логи показывают "SKIPPING PHONE LEAD CREATION"

2. **Форма FBI → Звонок I-9:**
   - PhoneCallLead создается (разные сервисы)
   - Синкается в I9_Verification

3. **Звонок → Форма:**
   - PhoneCallLead создается
   - Обновляется при заполнении формы
   - Переносится в "Order Received"
   - `order.zoho_synced = True`
   - НЕТ дубликатов в Zoho

4. **WhatConverts attribution:**
   - source, gclid, sentiment сохраняются
   - Lead Attribution Records создаются
   - call_duration, call_recording_url передаются

5. **Нет ошибок:**
   - Нет 500 errors в webhook
   - Нет ошибок в Zoho sync
   - Нет дубликатов в Zoho

---

## Final Check

После деплоя подождать 24 часа и проверить:

- [ ] Нет дубликатов в Zoho
- [ ] PhoneCallLead создаются только когда Order НЕ существует
- [ ] Матчинг работает только в рамках одного сервиса
- [ ] WhatConverts attribution сохраняется
- [ ] Нет ошибок в логах
- [ ] Managers не жалуются на дубликаты

---

## Contact

Если возникли проблемы:
1. Проверить логи: `railway logs` или `heroku logs --tail`
2. Проверить Django admin: Phone Call Leads, Orders
3. Проверить Zoho CRM: дубликаты, стадии
4. Проверить WhatConverts: webhook logs

---

## 🎉 Ready for Production!

После прохождения всех чеклистов система готова к продакшену.

Удачи! 🚀
