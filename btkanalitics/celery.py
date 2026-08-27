import logging
import os

from celery import Celery
from celery.signals import worker_shutting_down


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "btkanalitics.settings")


app = Celery("btkanalitics")

# Настройки берём из Django settings.py с префиксом CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Авто-поиск tasks.py во всех INSTALLED_APPS
app.autodiscover_tasks()


logger = logging.getLogger(__name__)


@worker_shutting_down.connect
def _fast_retry_active_parsers_on_shutdown(**kwargs) -> None:
    """При ПЛАНОВОМ (не аварийном) завершении воркера — сразу пометить ещё
    работавшие в этом процессе SSE-парсеры на быстрый перезапуск.

    ВАЖНО: используем именно worker_shutting_down, а не worker_shutdown.
    Первый вызывается СИНХРОННО в самом обработчике сигнала SIGTERM, в
    главном потоке, сразу при получении сигнала — до печати "Warm shutdown"
    (см. celery/apps/worker.py:_shutdown_handler). worker_shutdown, наоборот,
    срабатывает только по ЗАВЕРШЕНИИ штатного (warm) выключения — а для
    "вечных" SSE-задач (--pool=threads, очередь `parser`), которые никогда
    сами не завершаются, штатное выключение никогда не заканчивается:
    systemd просто убивает процесс по SIGKILL при достижении
    TimeoutStopSec, а SIGKILL вообще не даёт Python-коду выполниться —
    проверено на проде 27.08.2026, worker_shutdown ни разу не сработал.

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
            "worker_shutting_down: помечаю %s ещё активных парсеров на быстрый перезапуск: %s",
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
        logger.exception("worker_shutting_down: не удалось пометить активные парсеры")

