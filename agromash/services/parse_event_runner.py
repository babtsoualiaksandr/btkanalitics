import json
import logging
import random
import signal
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from agromash.models import AccountVideoAnalytics, Alarm, Monitor
from agromash.va_api_client import VAApiClient, VAAuthError


logger = logging.getLogger(__name__)


# Значения по умолчанию для SSE-парсера (можно переопределить в settings.py)
_DEFAULT_SSE_CONNECT_TIMEOUT = getattr(settings, "VA_SSE_CONNECT_TIMEOUT_SEC", 15.0)
_DEFAULT_SSE_READ_TIMEOUT = getattr(settings, "VA_SSE_READ_TIMEOUT_SEC", 60.0 * 60.0)
_DEFAULT_MAX_AUTH_FAILURES = getattr(settings, "VA_MAX_AUTH_FAILURES", 3)
_DEFAULT_MAX_SSE_FAILURES = getattr(settings, "VA_MAX_SSE_FAILURES", 10)


@dataclass
class ParserRunContext:
    account_id: int
    base_url: str
    # Частота, с которой будем перепроверять stop-флаг в БД, даже если поток «живой»
    stop_check_interval_sec: float = 2.0
    # Частота heartbeat (для индикации в админке)
    heartbeat_interval_sec: float = 5.0

    # Ограничение числа подряд неуспешных попыток аутентификации.
    # Нужно, чтобы при неверных кредах/429 не крутить бесконечный цикл login.
    max_auth_failures: int = _DEFAULT_MAX_AUTH_FAILURES

    # --- SSE tuning ---
    # requests timeout is (connect_timeout, read_timeout)
    # Для SSE read_timeout должен быть большим, иначе при отсутствии данных > N секунд
    # requests/urllib3 выбрасывают ReadTimeoutError.
    #
    # Настройки можно переопределить в settings.py:
    #   VA_SSE_CONNECT_TIMEOUT_SEC = 15.0  # таймаут на установку соединения
    #   VA_SSE_READ_TIMEOUT_SEC = 3600.0   # таймаут на чтение (1 час)
    sse_connect_timeout_sec: float = _DEFAULT_SSE_CONNECT_TIMEOUT
    sse_read_timeout_sec: float = _DEFAULT_SSE_READ_TIMEOUT

    # Backoff между переподключениями (если SSE рвётся по таймауту/сети)
    sse_reconnect_backoff_base_sec: float = 1.0
    sse_reconnect_backoff_max_sec: float = 30.0

    # Ограничение числа подряд неуспешных попыток SSE (короткоживущих сессий).
    # Если SSE рвётся слишком часто (< 30 сек) — считаем это критической ошибкой.
    max_sse_failures: int = _DEFAULT_MAX_SSE_FAILURES


def _mark_started(account_id: int, *, task_id: Optional[str]) -> None:
    now = timezone.now()
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING,
        parser_task_id=task_id,
        parser_stop_requested=False,
        parser_started_at=now,
        parser_stopped_at=None,
        parser_heartbeat_at=now,
        parser_last_error=None,
    )
    logger.info(
        "va_parser mark_started account_id=%s task_id=%s at=%s",
        account_id,
        task_id,
        now.isoformat(),
    )


def _mark_stopping(account_id: int) -> None:
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_STOPPING,
    )
    logger.info("va_parser mark_stopping account_id=%s", account_id)


def _mark_stopped(account_id: int) -> None:
    now = timezone.now()
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_STOPPED,
        parser_task_id=None,
        parser_stop_requested=False,
        parser_stopped_at=now,
        parser_heartbeat_at=now,
    )
    logger.info("va_parser mark_stopped account_id=%s at=%s", account_id, now.isoformat())


def _mark_error(account_id: int, *, error_text: str) -> None:
    now = timezone.now()
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
        parser_task_id=None,  # Сбрасываем task_id, чтобы можно было перезапустить
        parser_last_error=error_text[:5000],
        parser_stopped_at=now,
        parser_heartbeat_at=now,
    )

    # Stacktrace логируется в месте, где исключение поймано. Здесь — только причина.
    logger.error(
        "va_parser mark_error account_id=%s at=%s error=%s",
        account_id,
        now.isoformat(),
        (error_text or "")[:500],
    )


def _heartbeat(account_id: int) -> None:
    AccountVideoAnalytics.objects.filter(pk=account_id).update(parser_heartbeat_at=timezone.now())


def _stop_requested(account_id: int) -> bool:
    return (
        AccountVideoAnalytics.objects.filter(pk=account_id)
        .values_list("parser_stop_requested", flat=True)
        .first()
        is True
    )


def run_parse_event(
    *,
    account_id: int,
    task_id: Optional[str] = None,
    ctx: Optional[ParserRunContext] = None,
    stdout_write: Optional[Callable[[str], None]] = None,
) -> None:
    """Долгоживущий парсер событий для конкретного аккаунта.

    Важно:
      - Реальное состояние/остановка контролируются полями в AccountVideoAnalytics.
      - Остановка: выставить parser_stop_requested=True (и опционально terminate/revoke Celery).
    """
    ctx = ctx or ParserRunContext(account_id=account_id, base_url=settings.BASE_URL)
    write = stdout_write or (lambda s: logger.info(s))

    logger.info(
        "va_parser run_parse_event enter account_id=%s task_id=%s base_url=%s",
        account_id,
        task_id,
        ctx.base_url,
    )

    with transaction.atomic():
        acc = AccountVideoAnalytics.objects.select_for_update().get(pk=account_id)

        logger.info(
            "va_parser state_before_start account_id=%s status=%s task_id_db=%s stop_req=%s hb=%s",
            account_id,
            acc.parser_status,
            acc.parser_task_id,
            acc.parser_stop_requested,
            getattr(acc, "parser_heartbeat_at", None),
        )

        # Не допускаем параллельный запуск нескольких воркеров на один аккаунт.
        # Но важно: когда запуск инициирован из админки, статус уже может быть "starting".
        # В этом случае текущая Celery задача должна продолжить, если task_id совпадает.
        if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_RUNNING:
            write(f"Parser already in status=running, account_id={account_id}. Skip start.")
            return
        if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_STOPPING:
            write(f"Parser already in status=stopping, account_id={account_id}. Skip start.")
            return
        if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_STARTING:
            # Если это та же самая задача (или запуск через management command без task_id), продолжаем.
            if task_id and acc.parser_task_id and acc.parser_task_id != task_id:
                write(
                    f"Parser already in status=starting with another task_id={acc.parser_task_id}, "
                    f"account_id={account_id}. Skip start."
                )
                return

        # отметим, что запускаемся (быстро, до начала сетевых запросов)
        AccountVideoAnalytics.objects.filter(pk=account_id).update(
            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
            parser_task_id=task_id or acc.parser_task_id,
            parser_stop_requested=False,
            parser_last_error=None,
        )

        logger.info(
            "va_parser mark_starting account_id=%s task_id=%s",
            account_id,
            (task_id or acc.parser_task_id),
        )

    _mark_started(account_id, task_id=task_id)

    # Регистрируем обработчик SIGTERM (billiard отправляет его перед SIGKILL при time limit).
    # Это даёт нам шанс обновить статус в БД перед принудительным завершением.
    def _handle_sigterm(signum, frame):
        logger.warning(
            "va_parser received SIGTERM (likely time limit) account_id=%s task_id=%s",
            account_id,
            task_id,
        )
        try:
            AccountVideoAnalytics.objects.filter(pk=account_id).update(
                parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
                parser_last_error="Terminated by SIGTERM (time limit exceeded)",
                parser_stopped_at=timezone.now(),
                parser_heartbeat_at=timezone.now(),
            )
        except Exception:
            logger.exception("Failed to update status on SIGTERM")
        # Не вызываем sys.exit() — пусть процесс завершится естественно
    
    signal.signal(signal.SIGTERM, _handle_sigterm)

    client = VAApiClient(account_id=account_id, base_url=ctx.base_url)
    last_stop_check = 0.0
    last_hb = 0.0
    auth_failures = 0
    sse_failures = 0

    def should_stop(now_monotonic: float) -> bool:
        nonlocal last_stop_check
        if now_monotonic - last_stop_check < ctx.stop_check_interval_sec:
            return False
        last_stop_check = now_monotonic
        return _stop_requested(account_id)

    def maybe_heartbeat(now_monotonic: float) -> None:
        nonlocal last_hb
        if now_monotonic - last_hb < ctx.heartbeat_interval_sec:
            return
        last_hb = now_monotonic
        _heartbeat(account_id)

    try:
        while True:
            now_m = time.monotonic()
            maybe_heartbeat(now_m)
            if should_stop(now_m):
                _mark_stopping(account_id)
                break

            try:
                client.ensure_authenticated()
                auth_failures = 0
            except VAAuthError as e:
                auth_failures += 1
                write(f"Login failed ({auth_failures}/{ctx.max_auth_failures}): {e}")

                logger.warning(
                    "va_parser auth_failed account_id=%s task_id=%s failures=%s max=%s err=%s",
                    account_id,
                    task_id,
                    auth_failures,
                    ctx.max_auth_failures,
                    str(e),
                )

                if auth_failures >= int(ctx.max_auth_failures):
                    _mark_error(account_id, error_text=f"Auth failed {auth_failures} times: {e}")
                    return

                # Если API отдает 429 — уважаем Retry-After (если задан), иначе небольшой backoff.
                delay = getattr(e, 'retry_after_sec', None) or float(auth_failures)
                time.sleep(delay)
                continue
            except Exception as e:
                write(f"Login failed: {e}")
                time.sleep(1)
                continue

            # SSE loop
            sse_t0 = time.monotonic()
            sse_error: Optional[Exception] = None
            try:
                listen_sse(
                    client=client,
                    account_id=account_id,
                    should_stop=should_stop,
                    heartbeat=maybe_heartbeat,
                    write=write,
                    timeout=(float(ctx.sse_connect_timeout_sec), float(ctx.sse_read_timeout_sec)),
                )
            except VAAuthError as e:
                # SSE вернул 401 даже после login — считаем это auth failure
                sse_error = e
                auth_failures += 1
                logger.warning(
                    "va_parser sse_auth_failed account_id=%s task_id=%s failures=%s max=%s err=%s",
                    account_id,
                    task_id,
                    auth_failures,
                    ctx.max_auth_failures,
                    str(e),
                )
                if auth_failures >= int(ctx.max_auth_failures):
                    _mark_error(account_id, error_text=f"SSE auth failed {auth_failures} times: {e}")
                    return

            # Если stop был запрошен во время SSE — выходим.
            if _stop_requested(account_id):
                _mark_stopping(account_id)
                break

            # SSE разорван (таймаут/сеть/сервер). Делаем backoff перед переподключением.
            elapsed = time.monotonic() - sse_t0
            if elapsed >= 30.0:
                # Сессия прожила достаточно — считаем это "нормальным" обрывом, сбрасываем счётчики.
                sse_failures = 0
                auth_failures = 0
            else:
                sse_failures += 1

            # Проверяем лимит SSE failures (короткоживущих сессий)
            if sse_failures >= int(ctx.max_sse_failures):
                err_msg = f"SSE failed {sse_failures} times in a row (sessions < 30s)"
                if sse_error:
                    err_msg += f": {sse_error}"
                _mark_error(account_id, error_text=err_msg)
                return

            base = float(ctx.sse_reconnect_backoff_base_sec)
            max_d = float(ctx.sse_reconnect_backoff_max_sec)
            delay = min(max_d, base * (2 ** max(0, sse_failures - 1)))
            delay = delay + random.random() * 0.3  # небольшой джиттер
            logger.info(
                "va_parser sse_reconnect_scheduled account_id=%s in_sec=%.1f failures=%s last_elapsed_sec=%.1f",
                account_id,
                delay,
                sse_failures,
                elapsed,
            )

            slept = 0.0
            while slept < delay:
                now_m = time.monotonic()
                maybe_heartbeat(now_m)
                if should_stop(now_m):
                    _mark_stopping(account_id)
                    break
                step = min(1.0, delay - slept)
                time.sleep(step)
                slept += step

            if _stop_requested(account_id):
                _mark_stopping(account_id)
                break

        _mark_stopped(account_id)
    except Exception as e:
        logger.exception("parse_event failed for account_id=%s", account_id)
        _mark_error(account_id, error_text=str(e))
        raise


def listen_sse(
    *,
    client: VAApiClient,
    account_id: int,
    should_stop: Callable[[float], bool],
    heartbeat: Callable[[float], None],
    write: Callable[[str], None],
    timeout: tuple[float, float],
) -> None:
    sse_path = "/sse-holder/api/v1/sse?platform=WEB&ngsw-bypass"
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    response = None
    started_m = time.monotonic()
    try:
        logger.info(
            "va_parser sse_connect account_id=%s path=%s timeout=%s",
            account_id,
            sse_path,
            timeout,
        )
        response = client.request("GET", sse_path, headers=headers, stream=True, timeout=timeout)
        logger.info(
            "va_parser sse_connected account_id=%s status_code=%s",
            account_id,
            getattr(response, "status_code", None),
        )
        response.raise_for_status()

        event_type = None
        data = None

        for line in response.iter_lines(decode_unicode=True):
            now_m = time.monotonic()
            heartbeat(now_m)
            if should_stop(now_m):
                logger.info("va_parser sse_stop_requested account_id=%s", account_id)
                return

            if line == "":
                # End of SSE event
                if event_type and data:
                    _process_sse_event(
                        event_type=event_type,
                        data=data,
                        client=client,
                        account_id=account_id,
                        write=write,
                    )
                event_type = None
                data = None
                continue

            if line.startswith("event:"):
                event_type = line[6:]
                continue
            if line.startswith("data:"):
                data = line[5:]
                continue

    except VAAuthError:
        # Пробрасываем auth-ошибки наверх для корректного учёта auth_failures
        raise
    except Exception as e:
        elapsed = time.monotonic() - started_m
        write(f"Error in SSE: {e}")

        # Отдельно логируем, чтобы в journalctl было видно причину обрыва SSE.
        logger.warning(
            "va_parser sse_error account_id=%s err=%s elapsed_sec=%.1f timeout=%s",
            account_id,
            str(e),
            elapsed,
            timeout,
            exc_info=True,
        )
        return
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        logger.info("va_parser sse_disconnected account_id=%s", account_id)


def _process_sse_event(
    *,
    event_type: str,
    data: str,
    client: VAApiClient,
    account_id: int,
    write: Callable[[str], None],
) -> None:
    if event_type == "KEEP_ALIVE":
        try:
            parsed_data = json.loads(data)
        except json.JSONDecodeError:
            write(f"Invalid JSON in KEEP_ALIVE: {data}")
            logger.warning(
                "va_parser keep_alive_invalid_json account_id=%s data=%s",
                account_id,
                (data or "")[:300],
            )
            return

        ttl = parsed_data.get("ttl_seconds", 0)
        if ttl < 30:
            write(f"TTL {ttl} < 30, restarting stream")
            logger.warning("va_parser keep_alive_low_ttl account_id=%s ttl=%s", account_id, ttl)
            return
        # heartbeat делается выше
        return

    if event_type != "ALARM_MONITOR":
        # write(f"Unknown event type: {event_type}, data: {data}")
        return

    try:
        parsed_data = json.loads(data)
    except json.JSONDecodeError:
        write(f"Invalid JSON in ALARM_MONITOR: {data}")
        return

    monitor = parsed_data.get("monitor", {})
    monitor_id = monitor.get("id")
    monitor_name = monitor.get("name")
    if not monitor_id:
        return

    safe_monitor_name = monitor_name or ""
    safe_topic = monitor.get("topic", "") or ""

    # Create or update Monitor record
    monitor_obj, created = Monitor.objects.get_or_create(
        monitor_id=str(monitor_id),
        defaults={
            "monitor_name": safe_monitor_name,
            "topic": safe_topic,
        },
    )
    if not created:
        monitor_obj.monitor_name = safe_monitor_name
        monitor_obj.topic = safe_topic
        monitor_obj.save(update_fields=["monitor_name", "topic"])

    try:
        alarm_path = f"/api/v2/alarm-monitors/{monitor_id}/alarms/search"
        headers = {"Content-Type": "application/json"}
        payload = {"size": 1}
        resp = client.request("POST", alarm_path, headers=headers, json=payload)
        if resp.status_code != 200:
            write(f"Failed to get alarms for {monitor_id}: {resp.status_code}")
            return

        alarms = resp.json()
        account = AccountVideoAnalytics.objects.get(pk=account_id)
        for alarm in alarms:
            alarm_id = alarm.get("id")
            if not alarm_id:
                continue

            start_time = alarm.get("start_time")
            if start_time is None:
                # поле обязательное в модели, пропускаем некорректные записи
                continue
            end_time = alarm.get("end_time", start_time)

            Alarm.objects.get_or_create(
                alarm_id=alarm_id,
                account=account,
                defaults={
                    "monitor_id": alarm.get("monitor_id", monitor_id),
                    "monitor_name": alarm.get("monitor_name", monitor_name or ""),
                    "topic": alarm.get("topic", ""),
                    "start_time": start_time,
                    "end_time": end_time,
                    "event_id": alarm.get("event_id", 0),
                    "original_quality_snapshot": alarm.get("original_quality_snapshot"),
                    "plate_identities": alarm.get("plate_identities"),
                    "face_identities": alarm.get("face_identities"),
                    "snapshots": alarm.get("snapshots"),
                    "data": alarm,
                },
            )
    except Exception as e:
        write(f"Error getting alarms for monitor {monitor_id}: {e}")
