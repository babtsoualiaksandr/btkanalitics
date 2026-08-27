"""Мониторинг состояния парсеров и автоматическая коррекция рассинхронизации."""

import logging
from django.conf import settings
from django.utils import timezone
from celery import shared_task

from agromash.models import AccountVideoAnalytics


logger = logging.getLogger(__name__)

# Порог "протухшего" heartbeat (сек) — см. комментарий в settings.py.
PARSER_HEARTBEAT_TIMEOUT_SEC = getattr(settings, "PARSER_HEARTBEAT_TIMEOUT_SEC", 120)

# Backoff-лесенка перед автоматическим перезапуском (сек) — см. settings.py.
PARSER_RESTART_BACKOFF_SCHEDULE_SEC = getattr(
    settings, "PARSER_RESTART_BACKOFF_SCHEDULE_SEC", [30, 60, 180, 360, 600, 1800]
)

# Через сколько минут непрерывной успешной работы считаем эпизод флаппинга
# закрытым и сбрасываем счётчик попыток обратно в 0 (даже если он не
# понадобится в ближайшее время — чтобы следующее падение, если случится
# нескоро, снова пошло по быстрой ступени лесенки).
PARSER_SUSTAINED_SUCCESS_MIN = 3


def _backoff_delay_sec(attempt: int) -> int:
    schedule = PARSER_RESTART_BACKOFF_SCHEDULE_SEC
    idx = min(max(int(attempt) - 1, 0), len(schedule) - 1)
    return int(schedule[idx])


@shared_task(name="agromash.check_parser_heartbeats")
def check_parser_heartbeats() -> None:
    """Периодическая задача: проверяет heartbeat парсеров и корректирует зависшие статусы.

    Проблема:
    - Если Celery worker убивается (SIGKILL/TimeLimitExceeded), либо задача
      выполняется под пулом потоков (--pool=threads), где SIGTERM-обработчик
      не может сработать (см. agromash/services/parse_event_runner.py) —
      запись в БД не успевает обновиться. Остаётся parser_status=running,
      но heartbeat перестаёт обновляться.

    Решение:
    - Если parser_status=running И heartbeat старше PARSER_HEARTBEAT_TIMEOUT_SEC
      → переключаем в error (через _mark_error — эскалирует backoff-лесенку
      наравне с остальными видами падений).
    - Если, наоборот, parser_status=running достаточно долго и без сбоев —
      сбрасываем счётчик попыток (эпизод флаппинга закрыт).
    """

    from agromash.services.parse_event_runner import _mark_error

    now = timezone.now()
    stale_threshold = now - timezone.timedelta(seconds=PARSER_HEARTBEAT_TIMEOUT_SEC)

    stale_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING,
        parser_heartbeat_at__lt=stale_threshold,
    )
    stale_count = stale_qs.count()
    if stale_count:
        logger.warning(
            "check_parser_heartbeats: found %s stale parser(s) (heartbeat older than %ss)",
            stale_count,
            PARSER_HEARTBEAT_TIMEOUT_SEC,
        )
        for acc in stale_qs:
            last_hb = acc.parser_heartbeat_at or acc.parser_started_at or now
            age_sec = (now - last_hb).total_seconds()
            logger.warning(
                "check_parser_heartbeats: marking account_id=%s as error (heartbeat age: %.1f sec, task_id=%s)",
                acc.id,
                age_sec,
                acc.parser_task_id or "unknown",
            )
            _mark_error(acc.id, error_text=f"Heartbeat timeout (last heartbeat: {age_sec:.0f}s ago)")

    # Эпизод флаппинга считаем закрытым, если аккаунт работает без сбоев
    # достаточно долго — сбрасываем счётчик backoff-попыток.
    sustained_threshold = now - timezone.timedelta(minutes=PARSER_SUSTAINED_SUCCESS_MIN)
    recovered_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING,
        parser_heartbeat_at__gte=stale_threshold,
        parser_started_at__lt=sustained_threshold,
        parser_restart_attempt__gt=0,
    )
    recovered_count = recovered_qs.update(parser_restart_attempt=0)
    if recovered_count:
        logger.info(
            "check_parser_heartbeats: %s account(s) recovered (sustained %s+ min), backoff reset",
            recovered_count,
            PARSER_SUSTAINED_SUCCESS_MIN,
        )


@shared_task(name="agromash.auto_restart_error_parsers")
def auto_restart_error_parsers() -> None:
    """Периодическая задача: автоматически перезапускает парсеры в статусе error.

    Момент перезапуска определяется backoff-лесенкой (PARSER_RESTART_BACKOFF_SCHEDULE_SEC)
    по числу подряд идущих падений (parser_restart_attempt): первая попытка
    почти сразу, дальше пауза растёт, чтобы не долбить реально сломанный VA API.
    """

    now = timezone.now()

    # В error-статусе обычно единицы записей — фильтруем по минимальному
    # порогу на уровне БД (первая ступень лесенки), точный per-row порог
    # считаем в Python, т.к. он зависит от parser_restart_attempt каждой записи.
    min_delay_sec = min(PARSER_RESTART_BACKOFF_SCHEDULE_SEC)
    candidates_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
        parser_stopped_at__lt=now - timezone.timedelta(seconds=min_delay_sec),
    )

    to_restart = []
    for acc in candidates_qs:
        stopped_at = acc.parser_stopped_at or now
        age_sec = (now - stopped_at).total_seconds()
        needed_delay = _backoff_delay_sec(acc.parser_restart_attempt or 1)
        if age_sec >= needed_delay:
            to_restart.append((acc, age_sec, needed_delay))

    if not to_restart:
        logger.debug("auto_restart_error_parsers: no error parsers eligible yet")
        return

    logger.info(
        "auto_restart_error_parsers: found %s parser(s) eligible for restart",
        len(to_restart),
    )

    # Импортируем здесь, чтобы избежать циклических импортов
    from agromash.tasks import parse_event_task

    restarted = 0
    for acc, age_sec, needed_delay in to_restart:
        logger.info(
            "auto_restart_error_parsers: restarting parser for account_id=%s "
            "(attempt=%s, stopped %.1fs ago, needed %ss, last_error=%s)",
            acc.id,
            acc.parser_restart_attempt,
            age_sec,
            needed_delay,
            (acc.parser_last_error or "")[:100],
        )

        try:
            AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
                parser_last_error=f"Auto-restart after error (was: {(acc.parser_last_error or '')[:200]})",
            )

            result = parse_event_task.delay(account_id=acc.id)

            AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                parser_task_id=str(result.id) if result else None,
            )

            restarted += 1
            logger.info(
                "auto_restart_error_parsers: scheduled restart for account_id=%s task_id=%s",
                acc.id,
                result.id if result else None,
            )
        except Exception:
            logger.exception(
                "auto_restart_error_parsers: failed to restart parser for account_id=%s",
                acc.id,
            )
            AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
                parser_last_error=f"Auto-restart failed: {(acc.parser_last_error or '')[:200]}",
            )

    logger.info(
        "auto_restart_error_parsers: restarted %s/%s parser(s)",
        restarted,
        len(to_restart),
    )
