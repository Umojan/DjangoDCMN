# Fix: NameError - Optional is not defined

## ❌ Ошибка

```
NameError: name 'Optional' is not defined
File "/app/orders/services/attribution.py", line 340, in <module>
    def check_and_update_phone_lead(order, request) -> Optional['PhoneCallLead']:
```

## ✅ Исправление

Добавлен импорт `Optional` и `Dict` из `typing`:

```python
# БЫЛО:
from typing import Any

# СТАЛО:
from typing import Any, Optional, Dict
```

**Файл:** `django_dcmn/orders/services/attribution.py` (строка 14)

## 🔄 Что нужно сделать

1. **Перезапустить Django сервер:**
   ```bash
   # Если запущен через Railway/Heroku - автоматически перезапустится
   # Если локально:
   python manage.py runserver
   ```

2. **Проверить, что ошибка исчезла:**
   - Открыть любую страницу сайта
   - Или отправить тестовый webhook:
     ```bash
     curl -X POST http://localhost:8000/api/orders/webhook/whatconverts-test/ \
       -H "Content-Type: application/json" \
       -d '{"test": "data"}'
     ```

## 📝 Причина ошибки

При добавлении функции `check_and_update_phone_lead()` использовался type hint `Optional['PhoneCallLead']`, но забыли импортировать `Optional` из модуля `typing`.

Python требует явного импорта всех типов из `typing` модуля.

## ✅ Исправлено!

Теперь все импорты на месте:
- ✅ `Optional` - для optional return types
- ✅ `Dict` - для type hints словарей
- ✅ `Any` - для generic types

Ошибка больше не должна появляться.
