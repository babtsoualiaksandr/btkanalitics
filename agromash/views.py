from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
from django.http import JsonResponse
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.admin.views.decorators import staff_member_required
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_exempt
import subprocess
import os
import datetime
from typing import List, Tuple
from django.utils import timezone
from django.db.models import Max, Q

from .models import (
    AccountVideoAnalytics,
    Alarm,
    Monitor,
    ReportRunLog,
    TelegramEventLog,
)

from .va_api_client import VAApiClient

def start_parsing(request):
    if request.method == 'POST':
        pass
        return HttpResponse('Parsing started')
    return render(request, 'agromash/start_parsing.html')


def _to_aware_dt(value: int):
    """BigInteger epoch (sec or ms) -> aware datetime (UTC)."""
    if value is None:
        return None
    ts = int(value)
    if ts > 1_000_000_000_000:
        ts = ts / 1000.0
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


def _run_cmd(cmd: List[str], timeout_sec: int = 3) -> Tuple[int, str, str]:
    """Запуск команды без shell. Возвращает (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:
        return 1, "", str(e)


def _systemd_unit_state(unit: str) -> dict:
    rc, out, err = _run_cmd(
        [
            "systemctl",
            "show",
            unit,
            "--no-pager",
            "-p",
            "Id",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "UnitFileState",
        ],
        timeout_sec=3,
    )
    data = {"unit": unit, "rc": rc, "error": err.strip()}
    if rc != 0:
        return data

    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return data


def _journal_tail(*, unit: str, lines: int = 100) -> dict:
    cmd = [
        "journalctl",
        "-u",
        unit,
        "-n",
        str(int(lines)),
        "--no-pager",
        "-o",
        "short-iso",
    ]
    rc, out, err = _run_cmd(cmd, timeout_sec=5)
    return {"unit": unit, "cmd": " ".join(cmd), "rc": rc, "out": out, "err": err}


def _collect_system_status_context() -> dict:
    """Собрать данные для dashboard (переиспользуется обычной страницей и админкой)."""

    monitors = list(Monitor.objects.all().order_by("monitor_id"))

    # Последняя тревога по каждому monitor_id (Alarm.monitor_id = int).
    last_alarms = {
        row["monitor_id"]: row["last_start"]
        for row in Alarm.objects.values("monitor_id").annotate(last_start=Max("start_time"))
    }

    monitors_view = []
    now = timezone.now()
    day_ago = now - timezone.timedelta(hours=24)

    for m in monitors:
        try:
            mid_int = int(m.monitor_id)
        except (TypeError, ValueError):
            mid_int = None

        last_ts = last_alarms.get(mid_int) if mid_int is not None else None
        last_dt = _to_aware_dt(last_ts) if last_ts is not None else None
        last_dt_local = timezone.localtime(last_dt) if last_dt else None

        status = "unknown"
        if last_dt_local:
            status = "active" if last_dt_local >= day_ago else "stale"

        monitors_view.append(
            {
                "monitor_id": m.monitor_id,
                "monitor_name": m.monitor_name,
                "topic": m.topic,
                "last_alarm_at": last_dt_local,
                "status": status,
            }
        )

    units = [
        "btkanalitics.target",
        "btkanalitics-web.service",
        "btkanalitics-celery-worker.service",
        "btkanalitics-celery-beat.service",
    ]
    unit_states = [_systemd_unit_state(u) for u in units]

    service_units = [
        "btkanalitics-web.service",
        "btkanalitics-celery-worker.service",
        "btkanalitics-celery-beat.service",
    ]
    service_logs = [_journal_tail(unit=u, lines=100) for u in service_units]

    # Запущенные парсеры (best-effort)
    hb_threshold = timezone.now() - timezone.timedelta(minutes=2)
    running_parsers_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING
    ).filter(Q(parser_heartbeat_at__isnull=True) | Q(parser_heartbeat_at__gte=hb_threshold))
    running_parsers = list(running_parsers_qs.order_by("id"))

    # Последние события Telegram / отчёты
    telegram_logs = list(
        TelegramEventLog.objects.select_related("subscriber", "alarm").all()[:100]
    )
    report_logs = list(
        ReportRunLog.objects.select_related("subscriber", "subscription").all()[:100]
    )

    return {
        "monitors": monitors_view,
        "unit_states": unit_states,
        "service_logs": service_logs,
        "running_parsers": running_parsers,
        "telegram_logs": telegram_logs,
        "report_logs": report_logs,
    }


@staff_member_required
def system_status(request):
    """Страница статуса: мониторы + состояние systemd unit'ов + последние логи."""

    return render(request, "agromash/system_status.html", _collect_system_status_context())


@staff_member_required
def admin_system_status(request):
    """Та же страница статуса, но внутри стандартной Django admin."""

    ctx = django_admin.site.each_context(request)
    ctx.update(_collect_system_status_context())
    return TemplateResponse(request, "admin/system_status.html", ctx)


@staff_member_required
def admin_systemd_log(request):
    """AJAX endpoint: вернуть tail логов для одного systemd unit (для кнопки «Обновить»)."""

    unit = (request.GET.get("unit") or "").strip()
    allowed = {
        "btkanalitics-web.service",
        "btkanalitics-celery-worker.service",
        "btkanalitics-celery-beat.service",
    }
    if unit not in allowed:
        return JsonResponse({"error": "invalid unit"}, status=400)

    return JsonResponse(_journal_tail(unit=unit, lines=100))


@staff_member_required
def serve_snapshot(request, alarm_id):
    """
    View для отображения изображения с использованием Bearer токена
    """
    try:
        alarm = Alarm.objects.get(alarm_id=alarm_id)
    except Alarm.DoesNotExist:
        raise Http404("Alarm not found")
    
    # Важно для первичного запуска: если токенов ещё нет в БД,
    # VAApiClient сам выполнит login и сохранит их.
    if not alarm.original_quality_snapshot or not alarm.account:
        raise Http404("No snapshot available")
    
    try:
        client = VAApiClient(account_id=alarm.account_id, base_url=settings.BASE_URL)
        resp = client.request('GET', alarm.original_quality_snapshot, stream=True)

        if resp.status_code != 200:
            resp.close()
            raise Http404("Failed to fetch image")

        content_type = resp.headers.get('content-type', 'image/jpeg')
        content = resp.content
        resp.close()
        return HttpResponse(content, content_type=content_type)
    except Exception as e:
        raise Http404(f"Error fetching image: {str(e)}")
