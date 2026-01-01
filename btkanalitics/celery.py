import os

from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "btkanalitics.settings")


app = Celery("btkanalitics")

# Настройки берём из Django settings.py с префиксом CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Авто-поиск tasks.py во всех INSTALLED_APPS
app.autodiscover_tasks()

