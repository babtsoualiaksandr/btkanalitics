import logging
import time
import datetime
from typing import Optional

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.core.mail import EmailMessage

from agromash.models import AccountVideoAnalytics, ReportRunLog, TelegramReportSubscription
from agromash.services.parse_event_runner import ParserRunContext, run_parse_event
from agromash.services.report_scheduler import compute_next_run_at
from agromash.services.reporting import generate_report_attachments, generate_report_attachments_for_range
from agromash.services.telegram_client import send_document, send_message


logger = logging.getLogger(__name__)


@shared_task(bind=True, name="agromash.send_report_now")
def send_report_now(self, subscription_id: int, source: str = "admin") -> None:
    """Сформировать и отправить отчёт по конкретной подписке (ручной запуск).

    Делается в Celery, чтобы не блокировать HTTP (админку).
    """

    now = timezone.now()
    t0 = time.monotonic()

    sub = (
        TelegramReportSubscription.objects.select_related("subscriber")
        .prefetch_related("monitors")
        .filter(pk=subscription_id)
        .first()
    )
    if not sub or not sub.subscriber_id or not getattr(sub.subscriber, "chat_id", None):
        logger.warning("send_report_now: invalid subscription_id=%s", subscription_id)
        return

    run_log = ReportRunLog.objects.create(
        subscription=sub,
        subscriber=sub.subscriber,
        started_at=now,
        channel=ReportRunLog.CHANNEL_TELEGRAM,
    )

    try:
        caption, attachments, rows_count = generate_report_attachments(sub=sub, now=now)
        meta = {
            "source": source,
            "subscription_id": sub.id,
            "task_id": getattr(getattr(self, "request", None), "id", None),
        }

        if not attachments:
            send_message(chat_id=sub.subscriber.chat_id, text=caption, meta=meta)
        else:
            for idx, (filename, content, mime_type) in enumerate(attachments):
                send_document(
                    chat_id=sub.subscriber.chat_id,
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                    caption=caption if idx == 0 else None,
                    meta=meta,
                )

        sub.last_sent_at = now
        sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
        sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])

        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = True
        run_log.error = ""
        run_log.alarms_count = int(rows_count or 0)
        run_log.attachments_count = len(attachments or [])
        run_log.save(
            update_fields=[
                "finished_at",
                "duration_ms",
                "ok",
                "error",
                "alarms_count",
                "attachments_count",
            ]
        )
    except Exception:
        logger.exception("send_report_now failed (subscription_id=%s)", sub.id)
        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = False
        run_log.error = "exception"
        run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])


@shared_task(bind=True, name="agromash.send_report_range_now")
def send_report_range_now(
    self,
    subscription_id: int,
    start_iso: str,
    end_iso: str,
    source: str = "admin",
) -> None:
    """Сформировать и отправить отчёт по подписке за указанный диапазон start..end (ISO)."""

    sub = (
        TelegramReportSubscription.objects.select_related("subscriber")
        .prefetch_related("monitors")
        .filter(pk=subscription_id)
        .first()
    )
    if not sub or not sub.subscriber_id or not getattr(sub.subscriber, "chat_id", None):
        logger.warning("send_report_range_now: invalid subscription_id=%s", subscription_id)
        return

    try:
        start = datetime.datetime.fromisoformat(start_iso)
        end = datetime.datetime.fromisoformat(end_iso)
    except Exception:
        logger.exception("send_report_range_now: invalid datetime input")
        return

    now = timezone.now()
    t0 = time.monotonic()
    run_log = ReportRunLog.objects.create(
        subscription=sub,
        subscriber=sub.subscriber,
        started_at=now,
        channel=ReportRunLog.CHANNEL_TELEGRAM,
    )

    try:
        caption, attachments, rows_count = generate_report_attachments_for_range(
            sub=sub,
            start=start,
            end=end,
            now=now,
        )
        meta = {
            "source": source,
            "subscription_id": sub.id,
            "task_id": getattr(getattr(self, "request", None), "id", None),
            "range": f"{start_iso}..{end_iso}",
        }

        if not attachments:
            send_message(chat_id=sub.subscriber.chat_id, text=caption, meta=meta)
        else:
            for idx, (filename, content, mime_type) in enumerate(attachments):
                send_document(
                    chat_id=sub.subscriber.chat_id,
                    filename=filename,
                    content=content,
                    mime_type=mime_type,
                    caption=caption if idx == 0 else None,
                    meta=meta,
                )

        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = True
        run_log.error = ""
        run_log.alarms_count = int(rows_count or 0)
        run_log.attachments_count = len(attachments or [])
        run_log.save(
            update_fields=[
                "finished_at",
                "duration_ms",
                "ok",
                "error",
                "alarms_count",
                "attachments_count",
            ]
        )
    except Exception:
        logger.exception("send_report_range_now failed (subscription_id=%s)", sub.id)
        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = False
        run_log.error = "exception"
        run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])


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

        started_at = timezone.now()
        t0 = time.monotonic()
        run_log = ReportRunLog.objects.create(
            subscription=sub,
            subscriber=sub.subscriber,
            started_at=started_at,
            channel=ReportRunLog.CHANNEL_TELEGRAM,
        )

        try:
            caption, attachments, rows_count = generate_report_attachments(sub=sub, now=now)
            if not attachments:
                # если не удалось собрать файлы (нет библиотек) — хотя бы уведомим
                send_message(
                    chat_id=sub.subscriber.chat_id,
                    text=caption,
                    meta={"source": "celery", "subscription_id": sub.id},
                )
            else:
                for idx, (filename, content, mime_type) in enumerate(attachments):
                    send_document(
                        chat_id=sub.subscriber.chat_id,
                        filename=filename,
                        content=content,
                        mime_type=mime_type,
                        caption=caption if idx == 0 else None,
                        meta={"source": "celery", "subscription_id": sub.id},
                    )

            sub.last_sent_at = now
            sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
            sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])

            finished_at = timezone.now()
            run_log.finished_at = finished_at
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = True
            run_log.error = ""
            run_log.alarms_count = int(rows_count or 0)
            run_log.attachments_count = len(attachments or [])
            run_log.save(
                update_fields=[
                    "finished_at",
                    "duration_ms",
                    "ok",
                    "error",
                    "alarms_count",
                    "attachments_count",
                ]
            )
        except Exception:
            logger.exception("Ошибка отправки отчёта (subscription_id=%s)", sub.id)
            finished_at = timezone.now()
            run_log.finished_at = finished_at
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = False
            run_log.error = "exception"
            run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])


def _send_email_with_attachments(*, to_email: str, subject: str, body: str, attachments) -> None:
    msg = EmailMessage(
        subject=subject,
        body=body,
        to=[to_email],
    )
    for filename, content, mime_type in attachments or []:
        msg.attach(filename, content, mime_type)
    # fail_silently=False — ошибки должны попадать в логи/ReportRunLog
    msg.send(fail_silently=False)


@shared_task(bind=True, name="agromash.send_email_report_now")
def send_email_report_now(self, subscription_id: int, source: str = "admin") -> None:
    """Сформировать и отправить email-отчёт по конкретной подписке."""
    now = timezone.now()
    t0 = time.monotonic()

    sub = (
        TelegramReportSubscription.objects.select_related("subscriber")
        .prefetch_related("monitors")
        .filter(pk=subscription_id)
        .first()
    )
    if not sub:
        logger.warning("send_email_report_now: invalid subscription_id=%s", subscription_id)
        return
    if not sub.email:
        logger.warning("send_email_report_now: empty email for subscription_id=%s", sub.id)
        return

    run_log = ReportRunLog.objects.create(
        subscription=sub,
        subscriber=sub.subscriber if sub.subscriber_id else None,
        started_at=now,
        channel=ReportRunLog.CHANNEL_EMAIL,
    )

    try:
        caption, attachments, rows_count = generate_report_attachments(sub=sub, now=now)
        subject = f"BTK report: {rows_count} alarms"
        _send_email_with_attachments(
            to_email=str(sub.email),
            subject=subject,
            body=caption,
            attachments=attachments,
        )

        # next_run_at пересчитываем так же, как для Telegram
        sub.last_sent_at = now
        sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
        sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])

        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = True
        run_log.error = ""
        run_log.alarms_count = int(rows_count or 0)
        run_log.attachments_count = len(attachments or [])
        run_log.save(
            update_fields=[
                "finished_at",
                "duration_ms",
                "ok",
                "error",
                "alarms_count",
                "attachments_count",
            ]
        )
    except Exception:
        logger.exception("send_email_report_now failed (subscription_id=%s)", sub.id)
        run_log.finished_at = timezone.now()
        run_log.duration_ms = int((time.monotonic() - t0) * 1000)
        run_log.ok = False
        run_log.error = "exception"
        run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])


@shared_task(name="agromash.send_due_email_reports")
def send_due_email_reports() -> None:
    """Периодическая задача: отправляет email-отчёты подпискам, у которых подошёл срок."""
    now = timezone.now()

    due_qs = (
        TelegramReportSubscription.objects.filter(enabled=True)
        .exclude(email__isnull=True)
        .exclude(email="")
        .filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True))
        .select_related("subscriber")
        .prefetch_related("monitors")
        .order_by("id")
    )

    for sub in due_qs:
        if sub.next_run_at and sub.next_run_at > now:
            continue

        started_at = timezone.now()
        t0 = time.monotonic()
        run_log = ReportRunLog.objects.create(
            subscription=sub,
            subscriber=sub.subscriber if sub.subscriber_id else None,
            started_at=started_at,
            channel=ReportRunLog.CHANNEL_EMAIL,
        )

        try:
            caption, attachments, rows_count = generate_report_attachments(sub=sub, now=now)
            subject = f"BTK report: {rows_count} alarms"
            _send_email_with_attachments(
                to_email=str(sub.email),
                subject=subject,
                body=caption,
                attachments=attachments,
            )

            sub.last_sent_at = now
            sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
            sub.save(update_fields=["last_sent_at", "next_run_at", "updated_at"])

            finished_at = timezone.now()
            run_log.finished_at = finished_at
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = True
            run_log.error = ""
            run_log.alarms_count = int(rows_count or 0)
            run_log.attachments_count = len(attachments or [])
            run_log.save(
                update_fields=[
                    "finished_at",
                    "duration_ms",
                    "ok",
                    "error",
                    "alarms_count",
                    "attachments_count",
                ]
            )
        except Exception:
            logger.exception("Ошибка отправки email-отчёта (subscription_id=%s)", sub.id)
            finished_at = timezone.now()
            run_log.finished_at = finished_at
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = False
            run_log.error = "exception"
            run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])
