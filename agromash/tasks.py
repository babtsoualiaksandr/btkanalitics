import logging
from typing import Optional

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from django.conf import settings
from django.utils import timezone

from agromash.models import AccountVideoAnalytics
from agromash.services.parse_event_runner import ParserRunContext, run_parse_event


logger = logging.getLogger(__name__)


@shared_task(bind=True, name="agromash.parse_event")
def parse_event_task(self, account_id: int) -> None:
    """Celery-задача: запускает «вечный» парсер событий по аккаунту."""
    try:
        run_parse_event(
            account_id=account_id,
            task_id=getattr(self.request, "id", None),
            ctx=ParserRunContext(account_id=account_id, base_url=settings.BASE_URL),
        )
    except SoftTimeLimitExceeded:
        AccountVideoAnalytics.objects.filter(pk=account_id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
            parser_last_error="Soft time limit exceeded",
            parser_stopped_at=timezone.now(),
        )
        raise


def request_stop_parser(*, account_id: int, terminate: bool = True, signal: str = "SIGTERM") -> Optional[str]:
    """Запросить остановку парсера для аккаунта.

    Возвращает task_id, если он был известен.
    """
    task_id = (
        AccountVideoAnalytics.objects.filter(pk=account_id)
        .values_list("parser_task_id", flat=True)
        .first()
    )
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_stop_requested=True,
        parser_status=AccountVideoAnalytics.PARSER_STATUS_STOPPING,
    )

    if not task_id:
        return None

    if terminate:
        try:
            # current_app.control.revoke(...) не импортируем напрямую, чтобы избежать циклов
            from celery import current_app

            current_app.control.revoke(task_id, terminate=True, signal=signal)
        except Exception:
            logger.exception("Failed to revoke task_id=%s", task_id)
    return task_id


def is_task_active(task_id: str) -> bool:
    """Best-effort проверка активности таски (зависит от result backend)."""
    try:
        res = AsyncResult(task_id)
        return res.state in ("PENDING", "RECEIVED", "STARTED", "RETRY")
    except Exception:
        return False
