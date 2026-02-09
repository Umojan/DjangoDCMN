# ✅ Реверсивный матчинг: Форма → Звонок

## Что было реализовано

Теперь система работает в **обоих направлениях**:

### 1. ✅ Направление: Телефонный звонок → Форма (было раньше)
- Клиент звонит
- Создается PhoneCallLead с WhatConverts данными
- Клиент заполняет форму
- Система находит существующий PhoneCallLead по телефону + сервису
- Обновляет PhoneCallLead данными из формы
- Переносит в стадию "Order Received"
- Устанавливает `order.zoho_synced = True` для предотвращения дубликатов

### 2. ✅ Направление: Форма → Телефонный звонок (НОВОЕ)
- Клиент заполняет форму на сайте
- Создается Order (FBI, Marriage, I-9 и т.д.)
- Клиент звонит с вопросом
- WhatConverts отправляет webhook
- **Система находит существующий Order по телефону + сервису**
- **PhoneCallLead НЕ создается** (90% вероятность: уточняющий звонок)
- Если клиент хочет новую услугу, он заполнит форму сам

## Логика

### Почему не создавать Phone Lead, если Order уже существует?

**90% вероятность**: Звонок после заполнения формы = уточняющий вопрос

Примеры:
- "Когда будет готово?"
- "Какие документы нужны?"
- "Можно ли изменить адрес доставки?"
- "Как проверить статус?"

**Если клиент хочет НОВУЮ услугу:**
- Он уже знает как работает сайт
- Он просто заполнит новую форму
- Не нужно создавать Phone Lead

## Matching только в рамках одного сервиса

### ✅ Создается новый Phone Lead:
```
Форма: FBI Apostille (phone: +1234567890)
Звонок: I-9 Verification (phone: +1234567890)
→ Создается новый PhoneCallLead для I-9
```

### ⏭️ НЕ создается Phone Lead:
```
Форма: FBI Apostille (phone: +1234567890)
Звонок: FBI Apostille (phone: +1234567890)
→ PhoneCallLead НЕ создается (уточняющий звонок)
```

## Технические детали

### Изменения в `services/whatconverts.py`

#### 1. Функция `find_matching_order()` (строка ~138)

**Добавлен параметр `service_type`:**
```python
def find_matching_order(
    phone: str = None,
    email: str = None,
    service_type: str = None  # ← НОВОЕ
) -> Optional[Tuple[str, int, object]]:
    """
    Search for matching web form order by phone/email within the same service pipeline.

    CRITICAL: Only searches within the same service type.
    FBI phone call → Only matches FBI orders
    I-9 phone call → Only matches I-9 orders
    """
```

**Фильтрация по сервису:**
```python
# If service detected, only check that specific order type
if service_type:
    order_models = [
        (order_type, model)
        for order_type, model in order_models
        if order_type == service_type
    ]
    logger.info(f"🔍 Searching for orders in '{service_type}' pipeline only")
```

#### 2. Функция `process_whatconverts_phone_lead()` (строка ~281)

**Проверка ПЕРЕД созданием Phone Lead:**
```python
# CRITICAL: Check for existing web form order FIRST
# 90% probability: call is clarification about existing order
match = find_matching_order(
    phone=parsed['contact_phone'],
    email=parsed['contact_email'],
    service_type=parsed['detected_service']  # ← НОВОЕ: Только тот же сервис
)

if match:
    order_type, order_id, order_obj = match
    logger.info("=" * 80)
    logger.info(f"⏭️ SKIPPING PHONE LEAD CREATION")
    logger.info(f"   Found existing {order_type} order #{order_id}")
    logger.info(f"   90% probability: Clarification call about existing order")
    logger.info(f"   If customer wants NEW service, they'll fill out a form")
    logger.info("=" * 80)

    return None  # ← НЕ создаем Phone Lead
```

## Примеры работы

### Пример 1: Уточняющий звонок (Phone Lead НЕ создается)

**Шаги:**
1. Клиент заполняет форму FBI Apostille
   - Phone: +1 (555) 123-4567
   - Email: john@example.com
2. Создается `FbiApostilleOrder #123`
3. Клиент звонит через 2 часа с вопросом "когда будет готово?"
4. WhatConverts отправляет webhook:
   ```json
   {
     "lead_id": "WC-789",
     "contact_phone_number": "+1 (555) 123-4567",
     "landing_url": "https://dcmn.com/apostille-fbi-form"
   }
   ```
5. Django:
   - Парсит webhook
   - Детектирует service = 'fbi'
   - Ищет Order с phone=5551234567 AND service=fbi
   - **Находит FbiApostilleOrder #123**
   - **PhoneCallLead НЕ создается**
   - Логирует: "⏭️ SKIPPING PHONE LEAD CREATION"

### Пример 2: Новая услуга (Phone Lead создается)

**Шаги:**
1. Клиент заполняет форму FBI Apostille
   - Phone: +1 (555) 123-4567
2. Создается `FbiApostilleOrder #123`
3. Клиент звонит по поводу I-9 Verification
4. WhatConverts отправляет webhook:
   ```json
   {
     "landing_url": "https://dcmn.com/i-9-verification-form"
   }
   ```
5. Django:
   - Детектирует service = 'i9'
   - Ищет Order с phone=5551234567 AND service=i9
   - **НЕ находит** (есть только FBI order)
   - **Создает новый PhoneCallLead**
   - Синкает в Zoho → I9_Verification

### Пример 3: Звонок перед формой (работает как раньше)

**Шаги:**
1. Клиент звонит по поводу FBI Apostille
2. WhatConverts webhook → Django
3. Создается `PhoneCallLead #456`
4. Синкается в Zoho → FBI_Apostille (Stage: "Phone Call Received")
5. Клиент заполняет форму FBI Apostille
6. Django:
   - Создает `FbiApostilleOrder #124`
   - Находит PhoneCallLead #456 (phone + service)
   - Обновляет PhoneCallLead данными из формы
   - Обновляет Zoho Stage → "Order Received"
   - Устанавливает `order.zoho_synced = True`

## Проверка логов

При запуске вы увидите:

### Если Order существует (Phone Lead НЕ создается):
```
================================================================================
📞 Processing WhatConverts Phone Lead: WC-789
   Contact: John Doe | +1 (555) 123-4567
   Service: fbi
   Landing: https://dcmn.com/apostille-fbi-form
================================================================================
🔍 Searching for orders in 'fbi' pipeline only
✅ Found matching fbi order: 123
================================================================================
⏭️ SKIPPING PHONE LEAD CREATION
   Found existing fbi order #123
   Contact: John Doe | +1 (555) 123-4567
   90% probability: Clarification call about existing order
   If customer wants NEW service, they'll fill out a form
================================================================================
```

### Если Order НЕ существует (Phone Lead создается):
```
================================================================================
📞 Processing WhatConverts Phone Lead: WC-790
   Contact: Jane Smith | +1 (555) 987-6543
   Service: marriage
   Landing: https://dcmn.com/seal-marriage-form
================================================================================
🔍 Searching for orders in 'marriage' pipeline only
✓ No existing order found, proceeding with phone lead creation
✅ Created new phone lead: 12
```

## Итог

✅ **Направление 1: Звонок → Форма** (было раньше)
- PhoneCallLead создается
- При заполнении формы обновляется
- Переносится в "Order Received"

✅ **Направление 2: Форма → Звонок** (НОВОЕ)
- Order создается
- При звонке PhoneCallLead НЕ создается
- 90% вероятность: уточняющий звонок

✅ **Matching только в рамках одного сервиса**
- FBI → FBI ✅
- FBI → I-9 ❌

✅ **Нет дубликатов в Zoho**
- `order.zoho_synced = True` предотвращает дубли

✅ **Логика готова к продакшену**
