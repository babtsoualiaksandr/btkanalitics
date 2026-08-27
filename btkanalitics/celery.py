import logging
import os

from celery import Celery
from celery.signals import worker_shutdown


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "btkanalitics.settings")


app = Celery("btkanalitics")

# Настройки берём из Django settings.py с префиксом CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Авто-поиск tasks.py во всех INSTALLED_APPS
app.autodiscover_tasks()


logger = logging.getLogger(__name__)


@worker_shutdown.connect
def _fast_retry_active_parsers_on_shutdown(**kwargs) -> None:
    """При ПЛАНОВОМ (не аварийном) завершении воркера — сразу пометить ещё
    работавшие в этом процессе SSE-парсеры на быстрый перезапуск.

    worker_shutdown выполняется в главном потоке процесса воркера — в
    отличие от SIGTERM внутри дочерних потоков (--pool=threads, очередь
    `parser`), где наш обработчик не может зарегистрироваться (см.
    agromash/services/parse_event_runner.py). Без этого хука обнаружение
    происходило бы только по heartbeat-таймауту (минуты), плюс полная
    backoff-эскалация лесенки — это лишние минуты простоя мониторинга
    при каждом плановом рестарте/деплое.

    Для prefork-воркера (обычная очередь) этот реестр всегда пуст —
    parse_event-задачи там больше не выполняются (см. Фазу 3 рефакторинга),
    поэтому хук там безвредный no-op.
    """
    try:
        from agromash.services.parse_event_runner import get_active_account_ids, _mark_error

        account_ids = get_active_account_ids()
        if not account_ids:
            return

        logger.warning(
            "worker_shutdown: помечаю %s ещё активных парсеров на быстрый перезапуск: %s",
            len(account_ids),
            sorted(account_ids),
        )
        for account_id in account_ids:
            _mark_error(
                account_id,
                error_text="Graceful worker shutdown (deploy/restart)",
                force_fast_retry=True,
            )
    except Exception:
        logger.exception("worker_shutdown: не удалось пометить активные парсеры")

