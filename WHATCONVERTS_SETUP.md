# WhatConverts Integration - Quick Setup Guide

## 🚀 Быстрый старт

### 1. Применить миграцию базы данных

```bash
cd django_dcmn
../.venv/bin/python manage.py migrate orders
```

Ожидаемый вывод:
```
Running migrations:
  Applying orders.0026_phonecalllead... OK
```

---

### 2. Настроить Zoho CRM

#### A. Добавить стадию "Phone Call Received"

В каждом модуле (FBI Apostille, Marriage, Embassy, Translation, I-9, Notary, Apostille, Get a Quote):

1. Settings → Customization → Modules → [Выбрать модуль]
2. Pipeline → Edit Stages
3. Add New Stage:
   - **Name**: Phone Call Received
   - **Probability**: 40%
   - **Forecast Category**: Pipeline
4. Переместить на 1-е место (первая стадия после создания)
5. Save

#### B. Проверить поле Attribution_Record

Убедитесь что в модулях есть Lookup поле:
- **Field Name**: Attribution_Record
- **Type**: Lookup
- **Module**: Lead Attribution Records

---

### 3. Настроить WhatConverts Webhook

#### Войти в WhatConverts:
1. Profile Settings → Integrations
2. Add Webhook
3. Настройки:
   - **Name**: DCMN Django Production
   - **URL**: `https://yourdomain.com/api/orders/webhook/whatconverts/`
   - **Method**: POST
   - **Trigger**: New Leads
   - **Lead Types**: Phone Call only ✅
   - **Format**: JSON

#### Тестовый webhook (опционально):
- **URL**: `https://yourdomain.com/api/orders/webhook/whatconverts-test/`
- Логирует все данные без обработки

---

### 4. Протестировать интеграцию

#### Вариант A: Тестовый скрипт (рекомендуется)

```bash
cd /Users/mac/PycharmProjects/DjangoDCMN
python3 test_whatconverts_webhook.py
```

Выберите:
1. Test endpoint (1) - только логирование
2. Production endpoint (2) - полная обработка

#### Вариант B: Curl команда

```bash
# Test endpoint
curl -X POST http://localhost:8000/api/orders/webhook/whatconverts-test/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_type": "Phone Call",
    "lead_id": 999999,
    "contact_name": "Test User",
    "contact_phone_number": "+1-555-123-4567",
    "landing_url": "https://dcmn.us/apostille-fbi",
    "lead_source": "google",
    "date_created": "2026-02-04T15:30:00Z"
  }'
```

---

### 5. Проверить результаты

#### Django Admin
```
http://yourdomain.com/admin/orders/phonecalllead/
```

Должна появиться новая запись с:
- ✅ Contact name, phone
- ✅ Detected service: "fbi"
- ✅ Zoho synced: True
- ✅ Zoho lead ID
- ✅ Zoho attribution ID

#### Zoho CRM

1. Открыть модуль FBI Apostille (или другой определенный)
2. Найти лид со стадией "Phone Call Received"
3. Проверить:
   - ✅ Контактная информация заполнена
   - ✅ Lead Source = Google/Yelp/etc.
   - ✅ Description содержит AI summary
   - ✅ Related Lists → Attribution Records (должна быть привязана запись)

#### Логи Django

```bash
tail -f /path/to/django/logs/django.log | grep "WhatConverts"
```

Ожидаемые сообщения:
```
📞 Processing WhatConverts Phone Lead: 999999
✅ Detected service 'fbi' from URL: /apostille-fbi
✅ Created new phone lead: 123
📤 Syncing phone lead 123 to Zoho module: FBI_Apostille
✅ Created lead in Zoho FBI_Apostille: 5634000000123456
✅ Created attribution record: 5634000000789012
```

---

## 🧪 Тестовые сценарии

### Тест 1: FBI Apostille звонок

**Payload:**
```json
{
  "lead_type": "Phone Call",
  "lead_id": 101,
  "contact_name": "John Doe",
  "phone_number": "+1-555-123-4567",
  "email_address": "john@example.com",
  "landing_url": "https://dcmn.us/apostille-fbi",
  "lead_source": "google",
  "lead_score": 75
}
```

**Ожидается:**
- ✅ PhoneCallLead создан в Django
- ✅ detected_service = "fbi"
- ✅ Lead создан в FBI_Apostille модуле
- ✅ Stage = "Phone Call Received"
- ✅ Attribution Record создан

---

### Тест 2: Неопределенный сервис (homepage)

**Payload:**
```json
{
  "lead_type": "Phone Call",
  "lead_id": 102,
  "contact_name": "Jane Smith",
  "phone_number": "+1-555-987-6543",
  "landing_url": "https://dcmn.us/",
  "lead_source": "direct"
}
```

**Ожидается:**
- ✅ PhoneCallLead создан
- ✅ detected_service = "" (пусто)
- ✅ Lead создан в Get_a_Quote модуле
- ✅ Stage = "Phone Call Received"

---

### Тест 3: Tracking page (должен игнорироваться)

**Payload:**
```json
{
  "lead_type": "Phone Call",
  "lead_id": 103,
  "landing_url": "https://dcmn.us/tracking/ABC123"
}
```

**Ожидается:**
- ⏭️ Webhook отклонен
- ❌ PhoneCallLead НЕ создан
- Response: `{"status": "skipped", "reason": "Tracking page lead ignored"}`

---

### Тест 4: Web Form (не Phone Call)

**Payload:**
```json
{
  "lead_type": "Web Form",
  "lead_id": 104
}
```

**Ожидается:**
- ⏭️ Webhook отклонен
- ❌ PhoneCallLead НЕ создан
- Response: `{"status": "skipped", "reason": "Not a phone call lead"}`

---

### Тест 5: Дубликат (тот же номер)

**First call:**
```json
{
  "lead_type": "Phone Call",
  "lead_id": 105,
  "phone_number": "+1-555-111-2222",
  "landing_url": "https://dcmn.us/apostille-fbi"
}
```

**Second call (тот же номер):**
```json
{
  "lead_type": "Phone Call",
  "lead_id": 106,
  "phone_number": "+1-555-111-2222",
  "landing_url": "https://dcmn.us/translation-services"
}
```

**Ожидается:**
- ✅ Первый звонок → новый PhoneCallLead #1
- ✅ Второй звонок → обновляет PhoneCallLead #1
- ✅ Только 1 запись в Django (не дубликат)
- ✅ detected_service обновлен на "translation"

---

## 🔍 Проверка работы

### Чеклист после установки:

- [ ] Миграция применена (`PhoneCallLead` таблица создана)
- [ ] Стадия "Phone Call Received" добавлена во все модули Zoho
- [ ] Webhook настроен в WhatConverts
- [ ] Тестовый звонок обработался успешно
- [ ] Лид появился в Django Admin
- [ ] Лид появился в Zoho в правильном модуле
- [ ] Attribution Record создан и привязан
- [ ] Tracking страницы игнорируются
- [ ] Дубликаты не создаются

---

## 🚨 Troubleshooting

### Ошибка: "No module named 'django'"

```bash
# Используйте виртуальное окружение
cd django_dcmn
../.venv/bin/python manage.py migrate
```

### Webhook возвращает 500

1. Проверьте логи Django
2. Убедитесь что Zoho токен действителен
3. Проверьте что все required поля в Zoho настроены

### Лид не появляется в Zoho

1. Проверьте логи: `grep "Zoho" django.log`
2. Проверьте что модуль существует (FBI_Apostille, Marriage_Orders, etc.)
3. Проверьте ZohoCRMClient credentials

### Дубликаты создаются

1. Проверьте формат phone (должен быть одинаковый)
2. Проверьте email (case-sensitive check)
3. Проверьте логи на нормализацию номера

---

## 📚 Дополнительная документация

- **Полная документация**: `WHATCONVERTS_INTEGRATION.md`
- **Тестовый скрипт**: `test_whatconverts_webhook.py`

---

## ✅ Готово!

После выполнения всех шагов интеграция готова к работе.

Каждый новый звонок из WhatConverts:
1. Автоматически определит сервис по URL
2. Создаст лид в правильном модуле Zoho
3. Установит стадию "Phone Call Received"
4. Создаст Lead Attribution Record
5. Проверит на дубликаты
6. Найдет существующие заказы (если клиент уже заполнял форму)
