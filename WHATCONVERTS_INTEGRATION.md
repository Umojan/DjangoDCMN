# WhatConverts Phone Call Integration

## 📋 Обзор

Полная интеграция WhatConverts для обработки телефонных звонков с автоматическим определением сервиса, защитой от дубликатов и синхронизацией с Zoho CRM.

---

## 🎯 Функционал

### ✅ Реализовано:

1. **Модель PhoneCallLead** - хранение всех данных о звонках
2. **Автоматическое определение сервиса** по landing URL
3. **Get a Quote fallback** - неопределенные источники идут в Get a Quote
4. **Защита от дубликатов** по phone + service (только в том же пайплайне)
5. **Сопоставление с веб-формами** - находит phone leads и обновляет их
6. **Сохранение WhatConverts attribution** - при matching используется phone lead attribution
7. **Синхронизация с Zoho**:
   - Создание Lead/Deal в нужном модуле (или Get a Quote)
   - Stage: "Phone Call Received"
   - Создание Lead Attribution Record
   - Привязка attribution к лиду
   - Обновление stage на "Order Received" при matching
8. **Фильтрация**:
   - Только "Phone Call" тип лидов
   - Игнорирование /tracking страниц
   - Фильтр спама

---

## 🔧 Настройка

### 1. Применить миграцию

```bash
cd django_dcmn
../.venv/bin/python manage.py migrate orders
```

### 2. Настроить webhook в WhatConverts

**Production URL:**
```
https://yourdomain.com/api/orders/webhook/whatconverts/
```

**Test URL (для тестирования):**
```
https://yourdomain.com/api/orders/webhook/whatconverts-test/
```

### 3. Настроить стадии в Zoho

В каждом модуле добавьте стадию:
```
Stage: "Phone Call Received"
Probability: 40%
Forecast Category: Pipeline
```

**Модули:**
- FBI Apostille
- Marriage Orders
- Embassy Legalization
- Translation Services
- Apostille Orders
- I-9 Verification
- Notary Services
- Get a Quote (для неопределенных звонков)

---

## 📊 Определение сервиса по URL

### Routing таблица:

| Landing URL Pattern | Detected Service | Zoho Module |
|---------------------|-----------------|-------------|
| `/apostille-fbi`, `/apostille-fbi-form` | **fbi** | FBI_Apostille |
| `/triple-seal-marriage`, `/seal-marriage-form` | **marriage** | Marriage_Orders |
| `/embassy-legalization`, `/embassy-legalization-form` | **embassy** | Embassy_Legalization |
| `/translation-services`, `/translation-form` | **translation** | Translation_Services |
| `/apostille`, `/ssa-letter-apostille-services` | **apostille** | Apostille_Orders |
| `/i-9-verification-form`, `/i-9` | **i9** | I9_Verification |
| `/online-notary-form`, `/mobile-notary-services` | **notary** | Notary_Services |
| Другие URL | `None` | Deals (Get a Quote) |

### Игнорируются:
- `/tracking` - страницы отслеживания заказов

---

## 🔄 Workflow

### Сценарий 1: Звонок → Форма (стандартный)

```
1. Клиент звонит
   ↓
2. WhatConverts отправляет webhook
   ↓
3. Django создает PhoneCallLead
   ↓
4. Определяется сервис (FBI) по landing URL
   ↓
5. Создается Lead в FBI_Apostille модуле
   Stage: "Phone Call Received"
   ↓
6. Создается Lead Attribution Record
   ↓
7. Менеджер разговаривает с клиентом
   ↓
8. Клиент заполняет форму на сайте
   ↓
9. Django находит существующий PhoneCallLead по phone/email
   ↓
10. Обновляет существующий Lead в Zoho
   Stage: "Order Received"
```

### Сценарий 2: Только звонок (без формы)

```
1. Клиент звонит
   ↓
2. WhatConverts отправляет webhook
   ↓
3. Django создает PhoneCallLead + Zoho Lead
   Stage: "Phone Call Received"
   ↓
4. Менеджер работает с лидом в Zoho
   ↓
5. Вручную переводит в следующие стадии
```

### Сценарий 3: Неопределенный сервис

```
1. Клиент звонит с homepage
   ↓
2. Сервис не определен (нет специфичного URL)
   ↓
3. Создается Lead в "Get a Quote" модуле
   Stage: "Phone Call Received"
   ↓
4. Менеджер квалифицирует и перемещает в нужный модуль
```

---

## 🛡️ Защита от дубликатов

### Логика проверки:

1. **По WhatConverts lead_id** (основной ключ)
   - Если существует → обновляем запись

2. **По phone/email** (вторичная проверка)
   - Нормализация phone (последние 10 цифр)
   - Case-insensitive поиск по email
   - Если найден → обновляем запись

3. **Сопоставление с веб-формами**
   - Поиск по всем модулям заказов
   - Если найден заказ → помечаем `matched_with_form = True`
   - Сохраняем `matched_order_type` и `matched_order_id`

---

## 📝 Пример webhook данных

### WhatConverts отправляет:

```json
{
  "trigger": "new",
  "lead_id": 153928,
  "lead_type": "Phone Call",
  "contact_name": "John Doe",
  "contact_phone_number": "+18889703102",
  "contact_email_address": "john@example.com",
  "landing_url": "https://dcmn.us/apostille-fbi",
  "lead_source": "google",
  "lead_medium": "cpc",
  "lead_campaign": "fbi apostille",
  "lead_score": 75,
  "lead_analysis": {
    "Lead Summary": "Customer needs FBI apostille for job in Germany",
    "Sentiment Detection": "Positive",
    "Intent Detection": "Ready to purchase"
  },
  "city": "Charlotte",
  "state": "NC",
  "gclid": "CLibmtmqpNICFcSfGwodQbUAvg"
}
```

### Django создает PhoneCallLead:

```python
PhoneCallLead(
    whatconverts_lead_id="153928",
    contact_name="John Doe",
    contact_phone="+18889703102",
    contact_email="john@example.com",
    detected_service="fbi",  # ← Определено по URL
    landing_url="https://dcmn.us/apostille-fbi",
    source="google",
    medium="cpc",
    campaign="fbi apostille",
    lead_score=75,
    lead_summary="Customer needs FBI apostille for job in Germany",
    sentiment="Positive",
    zoho_module="FBI_Apostille",  # ← Целевой модуль
)
```

### В Zoho создается:

**FBI Apostille Lead:**
```json
{
  "First_Name": "John",
  "Last_Name": "Doe",
  "Email": "john@example.com",
  "Phone": "+18889703102",
  "Lead_Status": "Phone Call Received",
  "Lead_Source": "Google",
  "Service_Type": "FBI",
  "Rating": "Warm",
  "Description": "AI Summary: Customer needs FBI apostille for job in Germany\n\nIntent: Ready to purchase\n\nSentiment: Positive"
}
```

**Lead Attribution Record:**
```json
{
  "Name": "John Doe | google/cpc | 2026-02-04 15:30",
  "Lead_Type": "Phone",
  "Source": "google",
  "Source_Category": "Google",
  "Medium": "cpc",
  "Campaign": "fbi apostille",
  "Landing_Page": "https://dcmn.us/apostille-fbi",
  "City": "Charlotte",
  "State": "NC",
  "Attribution_Record": "<Lead_ID>"  // Lookup к лиду
}
```

---

## 🔍 Мониторинг и отладка

### Django Admin

Проверить созданные лиды:
```
http://admin.dcmn.us/admin/orders/phonecalllead/
```

### Логи

Все операции логируются:

```python
logger.info("📞 Processing WhatConverts Phone Lead: {lead_id}")
logger.info("✅ Detected service 'fbi' from URL: /apostille-fbi")
logger.info("🔄 Found existing phone lead by contact info")
logger.info("🔗 Phone lead matched with fbi order #123")
logger.info("✅ Created lead in Zoho FBI_Apostille: {zoho_id}")
logger.info("✅ Created attribution record: {attribution_id}")
```

### Тестирование

**1. Test webhook (логирование):**
```bash
curl -X POST https://dcmn.us/api/orders/webhook/whatconverts-test/ \
  -H "Content-Type: application/json" \
  -d '{
    "lead_type": "Phone Call",
    "lead_id": 999999,
    "contact_name": "Test User",
    "landing_url": "https://dcmn.us/apostille-fbi"
  }'
```

**2. Production webhook:**
```bash
curl -X POST https://dcmn.us/api/orders/webhook/whatconverts/ \
  -H "Content-Type: application/json" \
  -d @test_payload.json
```

---

## 📈 Вероятности конверсии

### По длительности звонка:

| Call Duration | Probability | Quality |
|--------------|-------------|---------|
| < 30 сек | 5% | Unqualified |
| 30-120 сек | 40% | Warm |
| 2-5 минут | 60% | Hot |
| > 5 минут | 65% | Very Hot |

### По lead score (WhatConverts):

| Lead Score | Rating | Probability |
|-----------|--------|-------------|
| 80-100 | Hot | 60% |
| 50-79 | Warm | 40% |
| 0-49 | Cold | 20% |

---

## 🚨 Troubleshooting

### Лид не создается в Zoho

1. Проверьте логи Django
2. Убедитесь что Zoho API токен валиден
3. Проверьте что модуль существует в Zoho
4. Проверьте что все required поля заполнены

### Дубликаты создаются

1. Проверьте что phone в одном формате
2. Убедитесь что email идентичен
3. Проверьте логи на наличие нормализации

### Сервис не определяется

1. Проверьте landing_url в webhook
2. Добавьте новый pattern в `SERVICE_URL_PATTERNS`
3. Убедитесь что URL не содержит /tracking

### Attribution не привязывается

1. Проверьте что Lookup поле настроено в Zoho
2. Убедитесь что lead_id валиден
3. Проверьте логи на ошибки создания attribution

---

## 📚 API Endpoints

### Production Webhook
```
POST /api/orders/webhook/whatconverts/
```

**Фильтры:**
- ✅ `lead_type == "Phone Call"`
- ✅ `/tracking` не в `landing_url`
- ✅ `spam == false`

**Response:**
```json
{
  "status": "success",
  "phone_lead_id": 123,
  "zoho_lead_id": "5634000000123456",
  "zoho_attribution_id": "5634000000789012",
  "detected_service": "fbi",
  "matched_with_form": false
}
```

### Test Webhook
```
POST /api/orders/webhook/whatconverts-test/
```

Логирует все данные без обработки.

---

## 🎯 Следующие шаги

### Рекомендации:

1. **Запустить миграцию:**
   ```bash
   cd django_dcmn
   ../.venv/bin/python manage.py migrate orders
   ```

2. **Настроить webhook в WhatConverts**

3. **Добавить стадию "Phone Call Received" во все модули Zoho**

4. **Протестировать с test webhook**

5. **Переключить на production webhook**

6. **Настроить мониторинг логов**

---

## 📞 Контакты

При возникновении вопросов проверьте:
- Django logs: `/var/log/django/`
- Webhook logs в WhatConverts dashboard
- PhoneCallLead в Django Admin

---

✅ **Интеграция готова к использованию!**
