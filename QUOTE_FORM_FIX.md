# Fix: Дубликаты лидов при matching с Phone Lead

## ❌ Проблема
Создавались 2 лида в Zoho вместо 1 при matching phone lead с формой.

## ✅ Исправление
Установка `order.zoho_synced = True` предотвращает дублирование.

**Файл:** `services/phone_lead_matcher.py`

```python
if phone_lead.zoho_lead_id:
    order_instance.zoho_synced = True
    order_instance.save(update_fields=['zoho_synced'])
```

## Результат
- ✅ Нет дубликатов
- ✅ Stage обновляется на "Order Received"
- ✅ Контактные данные обновляются
