# 🐛 Исправление Quote формы - ReferenceError: formData is not defined

## Проблема

В консоли браузера ошибка:
```
Uncaught (in promise) ReferenceError: formData is not defined
```

В HTML коде Quote формы (строка ~883):
```javascript
if (window.DCMNTracker) {
    formData.append("attribution", JSON.stringify(window.DCMNTracker.getAttribution()));
}
```

**Проблема:** Переменная `formData` не определена, но Quote форма отправляет **JSON**, а не FormData!

## Решение

### Найти в HTML (примерно строка 880-890):

```javascript
const payload = {
    name: nameInput.value,
    email: emailInput.value,
    phone: phoneInput.value,
    address: addressInput.value,
    number: numberInput.value,
    appointment_date: dateInput.value,
    appointment_time: timeInput.value,
    comments: commentsInput.value,
    services: selectedServices.join(", "),
};

if (window.DCMNTracker) {
    formData.append("attribution", JSON.stringify(window.DCMNTracker.getAttribution()));
}
```

### Заменить на:

```javascript
const payload = {
    name: nameInput.value,
    email: emailInput.value,
    phone: phoneInput.value,
    address: addressInput.value,
    number: numberInput.value,
    appointment_date: dateInput.value,
    appointment_time: timeInput.value,
    comments: commentsInput.value,
    services: selectedServices.join(", "),
};

// ✅ ИСПРАВЛЕНО: Добавляем attribution в payload как объект
if (window.DCMNTracker) {
    payload.attribution = window.DCMNTracker.getAttribution();
}
```

## Объяснение

### Было (неправильно):
```javascript
formData.append("attribution", JSON.stringify(...))  // formData не существует!
```

### Стало (правильно):
```javascript
payload.attribution = window.DCMNTracker.getAttribution()  // Добавляем в payload объект
```

## Почему это работает

Quote форма отправляет данные как **JSON**:
```javascript
body: JSON.stringify(payload)
```

Django получит:
```python
request.data = {
    'name': 'John',
    'email': 'john@example.com',
    'attribution': {
        'source': 'google',
        'medium': 'cpc',
        ...
    }
}
```

Django view уже обрабатывает это правильно благодаря исправлениям:
```python
# django_dcmn/orders/views/orders.py
order = serializer.save()
process_attribution(request, order)  # ✅ Уже добавлено
```

## Проверка других форм

Все остальные формы используют **FormData** и работают правильно:

### FBI (правильно):
```javascript
const formData = new FormData();
formData.append("name", nameInput.value);
if (window.DCMNTracker) {
    formData.append("attribution", JSON.stringify(window.DCMNTracker.getAttribution()));
}
```

### Marriage, Embassy, Translation, I-9 (правильно):
```javascript
const formData = new FormData();
if (window.DCMNTracker) {
    formData.append("attribution", JSON.stringify(window.DCMNTracker.getAttribution()));
}
```

Только **Quote форма** отличается - она отправляет JSON, поэтому нужно добавлять в `payload`, а не в `formData`.

## После исправления

1. Форма отправит attribution правильно
2. Django `process_attribution()` сохранит в order.attribution_data
3. При синхронизации с Zoho создастся Lead Attribution Record
4. Order будет привязан к Attribution Record

## Файл с исправленным кодом

См. `fix_quote_form.js` - полный исправленный обработчик формы.
