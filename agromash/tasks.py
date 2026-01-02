import logging
from typing import Optional

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from agromash.models import AccountVideoAnalytics, TelegramReportSubscription
from agromash.services.parse_event_runner import ParserRunContext, run_parse_event
from agromash.services.report_scheduler import compute_next_run_at
from agromash.services.reporting import generate_report_attachments
from agromash.services.telegram_client import send_document, send_message


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


@shared_task(name="agromash.send_due_telegram_reports")
def send_due_telegram_reports() -> None:
    """Периодическая задача: отправляет отчёты тем подпискам, у которых подошёл срок."""
    now = timezone.now()

    due_qs = (
        TelegramReportSubscription.objects.filter(enabled=True)
        .filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True))
        .select_related("subscriber")
        .prefetch_related("monitors")
        .order_by("id")
    )

    for sub in due_qs:
        # если next_run_at не задан — считаем, что можно отправить сразу
        if sub.next_run_at and sub.next_run_at > now:
            continue

        if not sub.subscriber_id or not getattr(sub.subscriber, "chat_id", None):
            continue

        try:
            caption, attachments = generate_report_attachments(sub=sub, now=now)
            if not attachments:
                # если не удалось собрать файлы (нет библиотек) — хотя бы уведомим
                send_message(chat_id=sub.subscriber.chat_id, text=caption)
            else:
                for idx, (filename, content, mime_type) in enumerate(attachments):
                    send_document(
                        chat_id=sub.subscriber.chat_id,
                        filename=filename,
                        content=content,
                        mime_type=mime_type,
                        caption=caption if idx == 0 else None,
                    )

            sub.last_sent_at = now
            sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
            sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])
        except Exception:
            logger.exception("Ошибка отправки отчёта (subscription_id=%s)", sub.id)
