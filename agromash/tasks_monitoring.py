"""Мониторинг состояния парсеров и автоматическая коррекция рассинхронизации."""

import logging
from django.conf import settings
from django.utils import timezone
from celery import shared_task

from agromash.models import AccountVideoAnalytics


logger = logging.getLogger(__name__)

# Время ожидания перед автоматическим перезапуском парсера в статусе error (минуты).
# Можно переопределить в settings: PARSER_AUTO_RESTART_DELAY_MIN
PARSER_AUTO_RESTART_DELAY_MIN = getattr(settings, "PARSER_AUTO_RESTART_DELAY_MIN", 5)


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


@shared_task(name="agromash.auto_restart_error_parsers")
def auto_restart_error_parsers() -> None:
    """Периодическая задача: автоматически перезапускает парсеры в статусе error.
    
    Логика:
    - Если parser_status=error И parser_stopped_at старше PARSER_AUTO_RESTART_DELAY_MIN минут
      → пытаемся перезапустить парсер.
    - Это позволяет автоматически восстановить работу после временных проблем
      (сетевые ошибки, проблемы на сервере VA и т.д.).
    
    Настройка:
    - PARSER_AUTO_RESTART_DELAY_MIN в settings (по умолчанию 5 минут).
    """
    
    now = timezone.now()
    delay_min = int(PARSER_AUTO_RESTART_DELAY_MIN)
    threshold = now - timezone.timedelta(minutes=delay_min)
    
    # Ищем аккаунты в статусе error, которые остановились достаточно давно
    error_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
        parser_stopped_at__lt=threshold,
    )
    
    error_count = error_qs.count()
    if not error_count:
        logger.debug("auto_restart_error_parsers: no error parsers to restart")
        return
    
    logger.info(
        "auto_restart_error_parsers: found %s parser(s) in error state (stopped > %s min ago)",
        error_count,
        delay_min,
    )
    
    # Импортируем здесь, чтобы избежать циклических импортов
    from agromash.tasks import parse_event_task
    
    restarted = 0
    for acc in error_qs:
        stopped_at = acc.parser_stopped_at or now
        age_sec = (now - stopped_at).total_seconds()
        
        logger.info(
            "auto_restart_error_parsers: restarting parser for account_id=%s (stopped %.1f sec ago, last_error=%s)",
            acc.id,
            age_sec,
            (acc.parser_last_error or "")[:100],
        )
        
        try:
            # Отмечаем, что начинаем перезапуск
            AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
                parser_last_error=f"Auto-restart after error (was: {(acc.parser_last_error or '')[:200]})",
            )
            
            # Запускаем задачу парсинга
            result = parse_event_task.delay(account_id=acc.id)
            
            # Сохраняем task_id
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
            # Возвращаем статус error, чтобы попробовать снова позже
            AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
                parser_last_error=f"Auto-restart failed: {(acc.parser_last_error or '')[:200]}",
            )
    
    logger.info(
        "auto_restart_error_parsers: restarted %s/%s parser(s)",
        restarted,
        error_count,
    )
