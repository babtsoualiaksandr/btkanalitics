import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from agromash.models import AccountVideoAnalytics, Alarm, Monitor
from agromash.va_api_client import VAApiClient


logger = logging.getLogger(__name__)


@dataclass
class ParserRunContext:
    account_id: int
    base_url: str
    # Частота, с которой будем перепроверять stop-флаг в БД, даже если поток «живой»
    stop_check_interval_sec: float = 2.0
    # Частота heartbeat (для индикации в админке)
    heartbeat_interval_sec: float = 5.0


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


def _mark_stopping(account_id: int) -> None:
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_STOPPING,
    )


def _mark_stopped(account_id: int) -> None:
    now = timezone.now()
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_STOPPED,
        parser_task_id=None,
        parser_stop_requested=False,
        parser_stopped_at=now,
        parser_heartbeat_at=now,
    )


def _mark_error(account_id: int, *, error_text: str) -> None:
    now = timezone.now()
    AccountVideoAnalytics.objects.filter(pk=account_id).update(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_ERROR,
        parser_last_error=error_text[:5000],
        parser_stopped_at=now,
        parser_heartbeat_at=now,
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

    with transaction.atomic():
        acc = AccountVideoAnalytics.objects.select_for_update().get(pk=account_id)

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

    _mark_started(account_id, task_id=task_id)

    client = VAApiClient(account_id=account_id, base_url=ctx.base_url)
    last_stop_check = 0.0
    last_hb = 0.0

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
            except Exception as e:
                write(f"Login failed: {e}")
                time.sleep(1)
                continue

            # SSE loop
            listen_sse(client=client, account_id=account_id, should_stop=should_stop, heartbeat=maybe_heartbeat, write=write)

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
) -> None:
    sse_path = "/sse-holder/api/v1/sse?platform=WEB&ngsw-bypass"
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    response = None
    try:
        response = client.request("GET", sse_path, headers=headers, stream=True)
        response.raise_for_status()

        event_type = None
        data = None

        for line in response.iter_lines(decode_unicode=True):
            now_m = time.monotonic()
            heartbeat(now_m)
            if should_stop(now_m):
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

    except Exception as e:
        write(f"Error in SSE: {e}")
        return
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


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
            return

        ttl = parsed_data.get("ttl_seconds", 0)
        if ttl < 30:
            write(f"TTL {ttl} < 30, restarting stream")
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
        payload = {"size": 2}
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
