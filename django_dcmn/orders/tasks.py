from celery import shared_task

@shared_task
def test_celery_task():
    print("Celery is working!")
    return "Hello from Celery"