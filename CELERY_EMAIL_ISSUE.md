# 🔍 Проблема: Email не приходят

## Диагностика

### 1. ✅ Celery Tasks настроены правильно
```python
# tasks.py (строка 85-91)
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=3,  # 3s, 6s, 12s, 24s...
    retry_backoff_max=60,
    retry_kwargs={'max_retries': 5},
)
def send_tracking_email_task(self, tid, stage_code):
    # ...
```

**Retry настроен:** До 5 попыток с экспоненциальным backoff

---

### 2. ❌ Celery Worker НЕ запущен

```bash
$ ps aux | grep celery
# Пусто - worker не запущен!
```

**Проблема:** Таски добавляются в очередь Redis, но **никто их не обрабатывает**

---

### 3. ⚠️ Resend Rate Limit (429)

```
[ERROR] Sending client email failed: Resend API response 429
"Too many requests. You can only make 2 requests per second."
```

**Что происходит:**
- Django пытается отправить email СИНХРОННО (не через Celery)
- Много email подряд → Rate limit
- Celery retry помог бы, но worker не запущен

---

## 🔧 Решения

### Проблема #1: Celery Worker не запущен

**Production (Railway/Heroku):**

Нужно добавить отдельный worker process в `Procfile`:

```bash
# Procfile
web: gunicorn django_dcmn.wsgi:application --bind 0.0.0.0:8080
worker: celery -A django_dcmn worker --loglevel=info
```

**Или в Railway dashboard:**
- Добавить новый Service
- Type: Worker
- Start Command: `celery -A django_dcmn worker --loglevel=info`

---

### Проблема #2: Email отправляется синхронно

**Проверь где вызывается email:**

```python
# Плохо (синхронно):
send_tracking_email_task(tid, stage_code)

# Хорошо (через Celery):
send_tracking_email_task.delay(tid, stage_code)
# или
send_tracking_email_task.apply_async(args=[tid, stage_code])
```

---

## 🧪 Как проверить

### 1. Локально запустить Celery worker:

```bash
# Terminal 1: Django
python manage.py runserver

# Terminal 2: Celery worker
celery -A django_dcmn worker --loglevel=info

# Terminal 3: Test
python manage.py shell
>>> from orders.tasks import send_tracking_email_task
>>> send_tracking_email_task.delay('TEST-123', 'created')
```

**Ожидаемый результат:**
- Terminal 2 (Celery) должен показать: "Task received", "Task succeeded"
- Email должен отправиться

---

### 2. Проверить Redis:

```bash
# Подключиться к Redis
redis-cli -u $REDIS_URL

# Посмотреть очередь задач
KEYS celery*
LLEN celery  # Сколько задач в очереди
```

**Если много задач в очереди:** Worker не обрабатывает

---

## 🚀 Быстрое решение для Production

### Railway:

1. **Dashboard → Settings → Deploy**
2. Добавить переменную окружения:
   ```
   CELERY_BROKER_URL = <ваш REDIS_URL>
   ```

3. **Dashboard → Services → New Service**
   - Name: `celery-worker`
   - Start Command: `celery -A django_dcmn worker --loglevel=info --concurrency=2`
   - Environment: Same as web service

4. **Deploy**

---

### Heroku:

```bash
# Procfile
web: gunicorn django_dcmn.wsgi:application
worker: celery -A django_dcmn worker --loglevel=info --concurrency=2

# Добавить worker dyno
heroku ps:scale worker=1

# Проверить логи
heroku logs --tail -p worker
```

---

## 📊 Мониторинг Celery

### Flower (Web UI для Celery):

```bash
# Установить
pip install flower

# Запустить
celery -A django_dcmn flower

# Открыть
http://localhost:5555
```

**В Flower видно:**
- Сколько задач в очереди
- Сколько выполнено/failed
- Время выполнения
- Worker статус

---

## ⚠️ Важные моменты

### 1. Rate Limit Protection

Celery retry уже настроен (строка 87-90):
```python
autoretry_for=(Exception,),  # Retry на любой Exception
retry_backoff=3,             # 3s, 6s, 12s, 24s, 48s
retry_backoff_max=60,        # Max 60s между попытками
max_retries=5,               # До 5 попыток
```

**Это защита от Resend 429 rate limit!**

---

### 2. Concurrency

```bash
# Низкая concurrency для email (избежать rate limit)
celery -A django_dcmn worker --concurrency=2
```

**Почему 2?**
- Resend limit: 2 requests/second
- Concurrency=2 → max 2 email одновременно
- Меньше шансов на 429

---

### 3. Проверить вызовы .delay()

Найти все места где email отправляется:

```bash
grep -r "send_tracking_email_task" --include="*.py"
```

**Убедиться что везде:**
```python
send_tracking_email_task.delay(tid, stage_code)  # ✅ Async
# НЕ:
send_tracking_email_task(tid, stage_code)  # ❌ Sync
```

---

## ✅ Чеклист

- [ ] Celery worker запущен (production)
- [ ] Redis подключен
- [ ] Email вызывается через `.delay()`
- [ ] Concurrency = 2 (для rate limit)
- [ ] Логи worker показывают обработку задач
- [ ] Flower настроен для мониторинга (опционально)

---

## 🎯 TL;DR

**Проблема:** Celery worker не запущен → таски в очереди, но не обрабатываются

**Решение:**
1. Добавить в `Procfile`: `worker: celery -A django_dcmn worker --loglevel=info --concurrency=2`
2. В Railway/Heroku: создать отдельный worker service
3. Проверить что email вызывается через `.delay()`

**После запуска worker:** Email начнут отправляться с retry при rate limit
