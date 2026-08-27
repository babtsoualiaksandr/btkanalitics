import logging
import time
import datetime
from typing import Optional

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.exceptions import TimeLimitExceeded
from celery.result import AsyncResult
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.mail import EmailMessage

from agromash.models import AccountVideoAnalytics, FuelReport, ReportRunLog, TelegramReportSubscription, TelegramSubscriber
from agromash.services.fuel_report_analyzer import analyze_fuel_report
from agromash.services.fuel_report_exporter import export_fuel_report_to_xlsx_bytes
from agromash.services.parse_event_runner import ParserRunContext, run_parse_event
from agromash.services.report_scheduler import compute_next_run_at
from agromash.services.reporting import generate_report_attachments, generate_report_attachments_for_range
from agromash.services.telegram_client import send_document, send_message

# Импортируем задачи мониторинга, чтобы Celery их зарегистрировал
from agromash import tasks_monitoring  # noqa: F401


logger = logging.getLogger(__name__)


@shared_task(bind=True, name="agromash.analyze_fuel_report")
def analyze_fuel_report_task(self, report_id: int, window_minutes: int = 10, source: str = "admin") -> None:
    """Celery-задача: выполнить анализ FuelOperation для конкретного FuelReport."""
    t0 = time.monotonic()
    task_id = getattr(getattr(self, "request", None), "id", None)

    # Отмечаем начало анализа
    FuelReport.objects.filter(pk=report_id).update(
        analysis_status=FuelReport.ANALYSIS_STATUS_PENDING,
        analysis_task_id=str(task_id or ""),
        analysis_error="",
    )

    def _report_progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        self.update_state(state="PROGRESS", meta={"current": done, "total": total, "percent": percent})

    try:
        summary = analyze_fuel_report(
            report_id=report_id,
            window_minutes=int(window_minutes),
            progress_cb=_report_progress,
        )

        FuelReport.objects.filter(pk=report_id).update(
            analysis_status=FuelReport.ANALYSIS_STATUS_DONE,
            analysis_error="",
            analysis_finished_at=timezone.now(),
        )

        logger.info(
            "analyze_fuel_report done report_id=%s updated=%s with_pi=%s with_alarms=%s alarms_candidates=%s source=%s task_id=%s elapsed_ms=%s",
            report_id,
            summary.operations_updated,
            summary.operations_with_plate_identity,
            summary.operations_with_alarms,
            summary.alarms_candidates,
            source,
            task_id,
            int((time.monotonic() - t0) * 1000),
        )
    except Exception as e:
        FuelReport.objects.filter(pk=report_id).update(
            analysis_status=FuelReport.ANALYSIS_STATUS_ERROR,
            analysis_error=str(e),
            analysis_finished_at=timezone.now(),
        )
        logger.exception(
            "analyze_fuel_report failed report_id=%s source=%s task_id=%s",
            report_id,
            source,
            task_id,
        )
        raise


@shared_task(bind=True, name="agromash.send_fuel_report_xlsx_to_subscribers")
def send_fuel_report_xlsx_to_subscribers(
    self,
    report_id: int,
    subscriber_ids: list[int],
    columns: Optional[list[str]] = None,
    source: str = "admin",
) -> None:
    """Сформировать XLSX по FuelReport и отправить выбранным TelegramSubscriber.

    Отправка делается:
      - в Telegram (document), если у подписчика есть chat_id
      - на email, если у подписчика задан TelegramSubscriber.email
    """

    started_at = timezone.now()
    t0 = time.monotonic()

    report = FuelReport.objects.filter(pk=report_id).first()
    if not report:
        logger.warning("send_fuel_report_xlsx_to_subscribers: report not found (report_id=%s)", report_id)
        return

    subs = list(
        TelegramSubscriber.objects.filter(pk__in=list(subscriber_ids)).order_by("id")
    )
    if not subs:
        logger.warning(
            "send_fuel_report_xlsx_to_subscribers: empty subscribers (report_id=%s)",
            report_id,
        )
        return

    try:
        content = export_fuel_report_to_xlsx_bytes(report_id=report.id, columns=columns)
    except Exception:
        logger.exception(
            "send_fuel_report_xlsx_to_subscribers: export failed (report_id=%s)",
            report.id,
        )
        return

    filename = f"fuel_report_{report.id}.xlsx"
    caption = f"FuelReport #{report.id}"
    if report.period_start and report.period_end:
        caption += f"\nПериод: {report.period_start}..{report.period_end}"
    if report.contract_number:
        caption += f"\nДоговор: {report.contract_number}"

    meta = {
        "source": source,
        "report_id": report.id,
        "task_id": getattr(getattr(self, "request", None), "id", None),
    }

    attachments = [
        (
            filename,
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    ]

    # best-effort: ошибки конкретного получателя не должны ронять остальные
    for sub in subs:
        # Telegram
        if getattr(sub, "chat_id", None):
            run_log = ReportRunLog.objects.create(
                subscription=None,
                subscriber=sub,
                started_at=started_at,
                channel=ReportRunLog.CHANNEL_TELEGRAM,
            )
            try:
                send_document(
                    chat_id=int(sub.chat_id),
                    filename=filename,
                    content=content,
                    mime_type=attachments[0][2],
                    caption=caption,
                    meta={**meta, "subscriber_id": sub.id},
                )
                run_log.finished_at = timezone.now()
                run_log.duration_ms = int((time.monotonic() - t0) * 1000)
                run_log.ok = True
                run_log.error = ""
                run_log.attachments_count = 1
                run_log.save(
                    update_fields=[
                        "finished_at",
                        "duration_ms",
                        "ok",
                        "error",
                        "attachments_count",
                    ]
                )
            except Exception:
                logger.exception(
                    "send_fuel_report_xlsx_to_subscribers: telegram send failed (report_id=%s, subscriber_id=%s)",
                    report.id,
                    sub.id,
                )
                run_log.finished_at = timezone.now()
                run_log.duration_ms = int((time.monotonic() - t0) * 1000)
                run_log.ok = False
                run_log.error = "exception"
                run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])

        # Email
        if getattr(sub, "email", None):
            run_log = ReportRunLog.objects.create(
                subscription=None,
                subscriber=sub,
                started_at=started_at,
                channel=ReportRunLog.CHANNEL_EMAIL,
            )
            try:
                subject = f"Fuel report #{report.id}"
                _send_email_with_attachments(
                    to_email=str(sub.email),
                    subject=subject,
                    body=caption,
                    attachments=attachments,
                )
                run_log.finished_at = timezone.now()
                run_log.duration_ms = int((time.monotonic() - t0) * 1000)
                run_log.ok = True
                run_log.error = ""
                run_log.attachments_count = 1
                run_log.save(
                    update_fields=[
                        "finished_at",
                        "duration_ms",
                        "ok",
                        "error",
                        "attachments_count",
                    ]
                )
            except Exception:
                logger.exception(
                    "send_fuel_report_xlsx_to_subscribers: email send failed (report_id=%s, subscriber_id=%s, email=%s)",
                    report.id,
                    sub.id,
                    sub.email,
                )
                run_log.finished_at = timezone.now()
                run_log.duration_ms = int((time.monotonic() - t0) * 1000)
                run_log.ok = False
                run_log.error = "exception"
                run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])


@shared_task(bind=True, name="agromash.generate_fuel_report_xlsx_cache")
def generate_fuel_report_xlsx_cache(self, report_id: int, columns: Optional[list[str]] = None, source: str = "admin") -> None:
    """Сформировать XLSX по FuelReport в фоне и сохранить bytes в FuelReport.export_xlsx_content."""

    t0 = time.monotonic()
    task_id = getattr(getattr(self, "request", None), "id", None)

    report = FuelReport.objects.filter(pk=report_id).first()
    if not report:
        logger.warning("generate_fuel_report_xlsx_cache: report not found report_id=%s", report_id)
        return

    # Отмечаем, что генерация начата (чтобы админка могла показать статус сразу)
    FuelReport.objects.filter(pk=report_id).update(
        export_xlsx_status=FuelReport.EXPORT_STATUS_PENDING,
        export_xlsx_task_id=str(task_id or ""),
        export_xlsx_error="",
    )

    try:
        content = export_fuel_report_to_xlsx_bytes(report_id=report_id, columns=columns)
        FuelReport.objects.filter(pk=report_id).update(
            export_xlsx_status=FuelReport.EXPORT_STATUS_READY,
            export_xlsx_generated_at=timezone.now(),
            export_xlsx_error="",
            export_xlsx_content=content,
        )
        logger.info(
            "generate_fuel_report_xlsx_cache done report_id=%s source=%s task_id=%s elapsed_ms=%s size=%s",
            report_id,
            source,
            task_id,
            int((time.monotonic() - t0) * 1000),
            len(content or b""),
        )
    except Exception as e:
        logger.exception(
            "generate_fuel_report_xlsx_cache failed report_id=%s source=%s task_id=%s",
            report_id,
            source,
            task_id,
        )
        FuelReport.objects.filter(pk=report_id).update(
            export_xlsx_status=FuelReport.EXPORT_STATUS_ERROR,
            export_xlsx_error=str(e),
        )
        return


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


@shared_task(bind=True, name="agromash.parse_event", time_limit=None, soft_time_limit=None)
def parse_event_task(self, account_id: int) -> None:
    """Celery-задача: запускает «вечный» парсер событий по аккаунту."""
    try:
        # Диагностический лог: какие лимиты видит воркер.
        # Это поможет подтвердить, что задачу убивает именно глобальный hard time limit.
        req = getattr(self, "request", None)
        logger.info(
            "parse_event_task start account_id=%s task_id=%s time_limit_req=%s soft_time_limit_req=%s settings_time_limit=%s settings_soft_time_limit=%s",
            account_id,
            getattr(req, "id", None),
            getattr(req, "time_limit", None),
            getattr(req, "soft_time_limit", None),
            getattr(settings, "CELERY_TASK_TIME_LIMIT", None),
            getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", None),
        )
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
    except TimeLimitExceeded:
        # Hard limit: воркер убивает задачу (обычно SIGKILL), поэтому важно успеть записать причину.
        # Это подтверждает, что лимит времени мешает «вечной» задаче.
        AccountVideoAnalytics.objects.filter(pk=account_id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
            parser_last_error="Hard time limit exceeded (TimeLimitExceeded)",
            parser_stopped_at=timezone.now(),
        )
        logger.exception(
            "parse_event_task hard time limit exceeded account_id=%s task_id=%s",
            account_id,
            getattr(getattr(self, "request", None), "id", None),
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

        # Claim-фаза: коротко блокируем строку (SELECT ... FOR UPDATE SKIP LOCKED)
        # и сразу сдвигаем next_run_at вперёд, прежде чем начинать медленную
        # отправку (сетевые вызовы к Telegram/VA API). Если параллельный запуск
        # этой же периодической задачи (например, если предыдущий не уложился
        # в 60-секундный интервал beat) уже забрал эту подписку — SKIP LOCKED
        # просто пропустит её здесь, без дублирующей отправки.
        planned_next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
        with transaction.atomic():
            claimed = (
                TelegramReportSubscription.objects.select_for_update(skip_locked=True)
                .filter(pk=sub.pk)
                .filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True))
            )
            if not claimed.exists():
                continue
            TelegramReportSubscription.objects.filter(pk=sub.pk).update(
                next_run_at=planned_next_run_at
            )

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

            # next_run_at уже сдвинут вперёд в claim-фазе выше.
            sub.last_sent_at = now
            sub.save(update_fields=["last_sent_at", "updated_at"])

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
            # Откатываем next_run_at, чтобы задача повторилась на следующем
            # цикле beat, как и раньше (next_run_at был сдвинут в claim-фазе).
            TelegramReportSubscription.objects.filter(pk=sub.pk).update(next_run_at=now)
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


@shared_task(bind=True, name="agromash.send_email_report_range_now")
def send_email_report_range_now(
    self,
    subscription_id: int,
    start_iso: str,
    end_iso: str,
    source: str = "admin",
) -> None:
    """Сформировать и отправить email-отчёт по подписке за указанный диапазон start..end (ISO)."""

    sub = (
        TelegramReportSubscription.objects.select_related("subscriber")
        .prefetch_related("monitors")
        .filter(pk=subscription_id)
        .first()
    )
    if not sub:
        logger.warning("send_email_report_range_now: invalid subscription_id=%s", subscription_id)
        return
    if not sub.email:
        logger.warning("send_email_report_range_now: empty email for subscription_id=%s", sub.id)
        return

    try:
        start = datetime.datetime.fromisoformat(start_iso)
        end = datetime.datetime.fromisoformat(end_iso)
    except Exception:
        logger.exception("send_email_report_range_now: invalid datetime input")
        return

    now = timezone.now()
    t0 = time.monotonic()
    run_log = ReportRunLog.objects.create(
        subscription=sub,
        subscriber=sub.subscriber if sub.subscriber_id else None,
        started_at=now,
        channel=ReportRunLog.CHANNEL_EMAIL,
    )

    try:
        caption, attachments, rows_count = generate_report_attachments_for_range(
            sub=sub,
            start=start,
            end=end,
            now=now,
        )
        subject = f"BTK report (range): {rows_count} alarms"
        _send_email_with_attachments(
            to_email=str(sub.email),
            subject=subject,
            body=caption,
            attachments=attachments,
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
        logger.exception("send_email_report_range_now failed (subscription_id=%s)", sub.id)
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

        # Claim-фаза: см. аналогичный комментарий в send_due_telegram_reports.
        planned_next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
        with transaction.atomic():
            claimed = (
                TelegramReportSubscription.objects.select_for_update(skip_locked=True)
                .filter(pk=sub.pk)
                .filter(Q(next_run_at__lte=now) | Q(next_run_at__isnull=True))
            )
            if not claimed.exists():
                continue
            TelegramReportSubscription.objects.filter(pk=sub.pk).update(
                next_run_at=planned_next_run_at
            )

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

            # next_run_at уже сдвинут вперёд в claim-фазе выше.
            sub.last_sent_at = now
            sub.save(update_fields=["last_sent_at", "updated_at"])

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
            # Откатываем next_run_at, чтобы задача повторилась на следующем
            # цикле beat, как и раньше (next_run_at был сдвинут в claim-фазе).
            TelegramReportSubscription.objects.filter(pk=sub.pk).update(next_run_at=now)
            finished_at = timezone.now()
            run_log.finished_at = finished_at
            run_log.duration_ms = int((time.monotonic() - t0) * 1000)
            run_log.ok = False
            run_log.error = "exception"
            run_log.save(update_fields=["finished_at", "duration_ms", "ok", "error"])
