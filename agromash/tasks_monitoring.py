"""Мониторинг состояния парсеров и автоматическая коррекция рассинхронизации."""

import logging
from django.utils import timezone
from celery import shared_task

from agromash.models import AccountVideoAnalytics


logger = logging.getLogger(__name__)


@shared_task(name="agromash.check_parser_heartbeats")
def check_parser_heartbeats() -> None:
    """Периодическая задача: проверяет heartbeat парсеров и корректирует зависшие статусы.
    
    Проблема:
    - Если Celery worker убивается (SIGKILL/TimeLimitExceeded), задача не успевает
      обновить parser_status в БД.
    - В результате остаётся parser_status=running, но heartbeat перестаёт обновляться.
    
    Решение:
    - Если parser_status=running И heartbeat старше HEARTBEAT_TIMEOUT_MIN минут
      → переключаем в error с пометкой "heartbeat timeout".
    """
    
    HEARTBEAT_TIMEOUT_MIN = 5
    now = timezone.now()
    threshold = now - timezone.timedelta(minutes=HEARTBEAT_TIMEOUT_MIN)
    
    # Ищем аккаунты со статусом running, но устаревшим heartbeat
    stale_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING,
        parser_heartbeat_at__lt=threshold,
    )
    
    stale_count = stale_qs.count()
    if not stale_count:
        logger.debug("check_parser_heartbeats: no stale parsers found")
        return
    
    logger.warning(
        "check_parser_heartbeats: found %s stale parser(s) (heartbeat older than %s min)",
        stale_count,
        HEARTBEAT_TIMEOUT_MIN,
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
        
        AccountVideoAnalytics.objects.filter(pk=acc.id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
            parser_last_error=f"Heartbeat timeout (last heartbeat: {age_sec:.0f}s ago)",
            parser_stopped_at=now,
            parser_heartbeat_at=now,
        )
    
    logger.info(
        "check_parser_heartbeats: corrected %s stale parser(s)",
        stale_count,
    )
