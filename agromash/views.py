from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
from django.http import JsonResponse
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_exempt
import subprocess
import os
from typing import List, Tuple
from django.utils import timezone
from django.db.models import Max, Q

from agromash.services.common import (
    alarm_epoch_to_aware_dt as _to_aware_dt,
    assert_events_access as _assert_events_access,
)

from .models import (
    AccountVideoAnalytics,
    Alarm,
    Monitor,
    ReportRunLog,
    TelegramEventLog,
    UserMonitorAccess,
)


from .va_api_client import VAApiClient


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
            "-p",
            "MemoryCurrent",
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

    # MemoryCurrent приходит в байтах (или "[not set]"/"18446744073709551615"
    # для юнитов без cgroup-учёта памяти) — сразу конвертируем в МБ для шаблона.
    mem_raw = data.get("MemoryCurrent")
    try:
        mem_bytes = int(mem_raw)
        # systemd возвращает UINT64_MAX ("18446744073709551615"), если учёт
        # памяти для юнита недоступен/не включён — это не реальное значение.
        data["memory_mb"] = round(mem_bytes / (1024 * 1024), 1) if 0 <= mem_bytes < 2**62 else None
    except (TypeError, ValueError):
        data["memory_mb"] = None

    return data


def _journal_tail(*, unit: str, lines: int = 100, since: str | None = None) -> dict:
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
    if since:
        cmd += ["--since", since]
    rc, out, err = _run_cmd(cmd, timeout_sec=5)
    return {"unit": unit, "cmd": " ".join(cmd), "rc": rc, "out": out, "err": err}


def _host_memory_summary() -> dict:
    """Память хоста из /proc/meminfo (без внешних команд/shell)."""
    try:
        raw = {}
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                key, _, rest = line.partition(":")
                # значения в /proc/meminfo — в КБ, вида "  16384000 kB"
                value_kb = rest.strip().split()[0]
                raw[key] = int(value_kb)

        total_kb = raw.get("MemTotal", 0)
        available_kb = raw.get("MemAvailable", 0)
        used_kb = max(total_kb - available_kb, 0)

        def _gb(kb: int) -> float:
            return round(kb / (1024 * 1024), 2)

        return {
            "total_gb": _gb(total_kb),
            "available_gb": _gb(available_kb),
            "used_gb": _gb(used_kb),
            "used_pct": round(100 * used_kb / total_kb, 1) if total_kb else None,
            "swap_total_gb": _gb(raw.get("SwapTotal", 0)),
            "swap_used_gb": _gb(raw.get("SwapTotal", 0) - raw.get("SwapFree", 0)),
            "error": None,
        }
    except Exception as e:
        return {
            "total_gb": None,
            "available_gb": None,
            "used_gb": None,
            "used_pct": None,
            "swap_total_gb": None,
            "swap_used_gb": None,
            "error": str(e),
        }


def _redis_queue_lengths() -> dict:
    """Длины очередей Celery в Redis (celery/parser) — best-effort.

    Растущая без потребителя очередь — явный сигнал проблемы (нет живого
    воркера на эту очередь, либо воркер завис/не успевает).
    """
    try:
        import redis

        client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        return {
            "celery": client.llen("celery"),
            "parser": client.llen("parser"),
            "error": None,
        }
    except Exception as e:
        return {"celery": None, "parser": None, "error": str(e)}


def _active_tasks_worker_by_id(timeout_sec: float = 2.0) -> dict:
    """task_id -> hostname воркера, который сейчас реально его выполняет.

    Через Celery control API (broadcast всем воркерам, best-effort). Нужно,
    чтобы поймать рассинхрон: аккаунт помечен в БД как запущенный, но его
    parse_event-задача крутится не на том воркере (см. инцидент 2026-08-27 —
    после общего рестарта пять аккаунтов на несколько минут оказались на
    обычном prefork-воркере вместо `parser`).
    """
    from btkanalitics.celery import app as celery_app

    result: dict = {}
    try:
        active = celery_app.control.inspect(timeout=timeout_sec).active() or {}
        for worker_name, tasks in active.items():
            for t in tasks:
                task_id = t.get("id")
                if task_id:
                    result[task_id] = worker_name
    except Exception:
        pass
    return result


def _filter_log_lines(text: str, *, must_contain: list[str]) -> str:
    """Фильтрация логов по ключевым словам (все слова должны встретиться).

    - Регистр не важен.
    - Пустые токены игнорируются.
    """

    raw_lines = (text or "").splitlines()
    tokens = [t.strip().lower() for t in (must_contain or []) if str(t or "").strip()]
    if not tokens:
        return "\n".join(raw_lines)

    out_lines: list[str] = []
    for line in raw_lines:
        ll = line.lower()
        if all(tok in ll for tok in tokens):
            out_lines.append(line)
    return "\n".join(out_lines)


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
        "btkanalitics-celery-worker-parser.service",
        "btkanalitics-celery-beat.service",
        # Инфраструктурные зависимости — не часть btkanalitics.target, но
        # без них приложение не работает вообще, а их падение выглядело бы
        # отсюда просто как "всё сломалось" без единой зацепки.
        "nginx.service",
        "postgresql@16-main.service",
        "redis-server.service",
        # Telegram-алертер (P2-4) — шаблонный oneshot-юнит, инстанс без
        # аргумента невалиден для systemctl show, поэтому подставляем
        # фиктивный instance-суффикс. ActiveState=inactive/SubState=dead —
        # это НОРМА для этого юнита (он живёт только момент срабатывания
        # OnFailure), важно только UnitFileState=static (шаблон установлен).
        "btkanalitics-alert@status-check.service",
    ]
    unit_states = [_systemd_unit_state(u) for u in units]

    service_units = [
        "btkanalitics-web.service",
        "btkanalitics-celery-worker.service",
        "btkanalitics-celery-worker-parser.service",
        "btkanalitics-celery-beat.service",
    ]
    service_logs = [_journal_tail(unit=u, lines=100) for u in service_units]

    # --- Память хоста и Redis-очереди (диагностика ресурсов/затыков) ---
    host_memory = _host_memory_summary()
    redis_queues = _redis_queue_lengths()

    # --- VA parser lifecycle logs (для диагностики рассинхрона статуса/кнопки) ---
    # Ищем именно те строки, которые пишет [`agromash/services/parse_event_runner.py`](agromash/services/parse_event_runner.py:1)
    # с префиксом "va_parser". С Фазы 3 (2026-08-27) эти задачи должны жить
    # ТОЛЬКО на btkanalitics-celery-worker-parser.service (очередь `parser`,
    # пул потоков) — обычный воркер их видеть не должен вообще.
    #
    # Фильтрацию по ключевым словам делаем на сервере (GET-параметр va_log_q)
    # и дополнительно на клиенте (JS) в шаблоне.
    worker_unit = "btkanalitics-celery-worker-parser.service"
    worker_tail = _journal_tail(unit=worker_unit, lines=500)
    raw_worker_out = str(worker_tail.get("out") or "")
    va_lines = "\n".join([ln for ln in raw_worker_out.splitlines() if "va_parser" in ln])
    # query будет добавлен в ctx ниже из request (см. system_status/admin_system_status)
    va_parser_logs = {
        "unit": worker_unit,
        "cmd": worker_tail.get("cmd"),
        "rc": worker_tail.get("rc"),
        "err": worker_tail.get("err"),
        "raw_count": len(raw_worker_out.splitlines()),
        "va_count": len(va_lines.splitlines()) if va_lines else 0,
        "text": va_lines,
        "q": "",
    }

    # --- Авто-проверка на неправильный роутинг парсеров ---
    # 27.08.2026 после общего рестарта пять аккаунтов на несколько минут
    # оказались на обычном prefork-воркере вместо `parser` (см. память
    # рефакторинга). Признак ровно тот же: строки "va_parser" в логе
    # обычного воркера, где их вообще не должно быть.
    misrouted_unit = "btkanalitics-celery-worker.service"
    misrouted_window = "30 minutes ago"
    misrouted_tail = _journal_tail(unit=misrouted_unit, lines=2000, since=misrouted_window)
    misrouted_raw = str(misrouted_tail.get("out") or "")
    misrouted_lines = "\n".join([ln for ln in misrouted_raw.splitlines() if "va_parser" in ln])
    routing_check = {
        "unit": misrouted_unit,
        "window": misrouted_window,
        "has_misrouted": bool(misrouted_lines),
        "count": len(misrouted_lines.splitlines()) if misrouted_lines else 0,
        "text": misrouted_lines,
    }

    # --- Запущенные парсеры (best-effort) + реальный воркер по task_id ---
    active_task_workers = _active_tasks_worker_by_id()
    hb_threshold = timezone.now() - timezone.timedelta(minutes=2)
    running_parsers_qs = AccountVideoAnalytics.objects.filter(
        parser_status=AccountVideoAnalytics.PARSER_STATUS_RUNNING
    ).filter(Q(parser_heartbeat_at__isnull=True) | Q(parser_heartbeat_at__gte=hb_threshold))
    running_parsers = list(running_parsers_qs.order_by("id"))
    for acc in running_parsers:
        worker_name = active_task_workers.get(acc.parser_task_id)
        acc.current_worker = worker_name or "?"
        # Ожидаем hostname вида "parser@ServerForIpCam" (см. --hostname в
        # btkanalitics-celery-worker-parser.service). Если задача крутится
        # на воркере с другим именем — это и есть рассинхрон маршрутизации.
        acc.worker_mismatch = bool(worker_name) and not worker_name.startswith("parser@")

    # Последние события Telegram / отчёты
    telegram_logs = list(
        TelegramEventLog.objects.select_related("subscriber", "alarm").all()[:100]
    )
    report_logs = list(
        ReportRunLog.objects.select_related("subscriber", "subscription").all()[:100]
    )

    # Активные задачи анализа FuelReport
    from agromash.models import FuelReport
    from agromash.tasks import is_task_active

    analyzing_reports = []
    for fr in FuelReport.objects.filter(
        analysis_status=FuelReport.ANALYSIS_STATUS_PENDING
    ).order_by('-id')[:20]:
        task_active = False
        task_id = getattr(fr, "analysis_task_id", "") or ""
        if task_id:
            try:
                task_active = is_task_active(task_id)
            except Exception:
                pass
        analyzing_reports.append({
            'id': fr.id,
            'contract': fr.contract_number or '',
            'organization': fr.organization_name or '',
            'period_start': fr.period_start,
            'period_end': fr.period_end,
            'task_id': task_id,
            'task_active': task_active,
            'created_at': fr.created_at,
        })

    return {
        "monitors": monitors_view,
        "unit_states": unit_states,
        "service_logs": service_logs,
        "host_memory": host_memory,
        "redis_queues": redis_queues,
        "routing_check": routing_check,
        "va_parser_logs": va_parser_logs,
        "running_parsers": running_parsers,
        "telegram_logs": telegram_logs,
        "report_logs": report_logs,
        "analyzing_reports": analyzing_reports,
    }


@staff_member_required
def system_status(request):
    """Страница статуса: мониторы + состояние systemd unit'ов + последние логи."""

    ctx = _collect_system_status_context()
    q = str(request.GET.get("va_log_q") or "").strip()
    ctx["va_parser_logs"]["q"] = q
    if q:
        ctx["va_parser_logs"]["text"] = _filter_log_lines(
            ctx["va_parser_logs"]["text"],
            must_contain=["va_parser", *q.split()],
        )
    return render(request, "agromash/system_status.html", ctx)


@staff_member_required
def admin_system_status(request):
    """Та же страница статуса, но внутри стандартной Django admin."""

    ctx = django_admin.site.each_context(request)
    ctx.update(_collect_system_status_context())
    q = str(request.GET.get("va_log_q") or "").strip()
    ctx["va_parser_logs"]["q"] = q
    if q:
        ctx["va_parser_logs"]["text"] = _filter_log_lines(
            ctx["va_parser_logs"]["text"],
            must_contain=["va_parser", *q.split()],
        )
    return TemplateResponse(request, "admin/system_status.html", ctx)


@staff_member_required
def admin_systemd_log(request):
    """AJAX endpoint: вернуть tail логов для одного systemd unit (для кнопки «Обновить»)."""

    unit = (request.GET.get("unit") or "").strip()
    allowed = {
        "btkanalitics-web.service",
        "btkanalitics-celery-worker.service",
        "btkanalitics-celery-worker-parser.service",
        "btkanalitics-celery-beat.service",
    }
    if unit not in allowed:
        return JsonResponse({"error": "invalid unit"}, status=400)

    return JsonResponse(_journal_tail(unit=unit, lines=100))


@staff_member_required
def admin_task_status(request):
    """AJAX endpoint: статус Celery-задачи + статус FuelReport (если передан report_id).

    Параметры GET:
      - task_id: идентификатор Celery-задачи (опционально)
      - report_id: PK FuelReport (опционально)

    Возвращает JSON с полями:
      - task_id, state, ready, successful, error, meta  (из Celery result backend)
      - report.analysis_status, report.analysis_error, ...
      - report.export_xlsx_status, report.export_xlsx_error, ...
    """
    from celery.result import AsyncResult
    from agromash.models import FuelReport

    task_id = (request.GET.get("task_id") or "").strip()
    report_id = request.GET.get("report_id")

    data: dict = {}

    # 1) Статус из Celery result backend (Redis)
    if task_id:
        res = AsyncResult(task_id)
        data["task_id"] = task_id
        data["state"] = res.state  # PENDING / STARTED / SUCCESS / FAILURE / RETRY / PROGRESS
        data["ready"] = res.ready()
        data["successful"] = res.successful() if res.ready() else None
        if res.failed():
            data["error"] = str(res.result)
        # meta (если задача пишет self.update_state)
        if isinstance(res.info, dict):
            data["meta"] = res.info

    # 2) Статус из модели FuelReport
    if report_id:
        try:
            rpt = FuelReport.objects.filter(pk=int(report_id)).values(
                "analysis_status",
                "analysis_error",
                "analysis_task_id",
                "analysis_finished_at",
                "export_xlsx_status",
                "export_xlsx_error",
                "export_xlsx_generated_at",
                "export_xlsx_task_id",
            ).first()
            if rpt:
                data["report"] = {
                    "analysis_status": rpt["analysis_status"],
                    "analysis_error": rpt["analysis_error"] or "",
                    "analysis_task_id": rpt["analysis_task_id"] or "",
                    "analysis_finished_at": (
                        rpt["analysis_finished_at"].isoformat()
                        if rpt["analysis_finished_at"] else None
                    ),
                    "export_xlsx_status": rpt["export_xlsx_status"],
                    "export_xlsx_error": rpt["export_xlsx_error"] or "",
                    "export_xlsx_generated_at": (
                        rpt["export_xlsx_generated_at"].isoformat()
                        if rpt["export_xlsx_generated_at"] else None
                    ),
                    "export_xlsx_task_id": rpt["export_xlsx_task_id"] or "",
                }
        except Exception:
            pass

    return JsonResponse(data)


@login_required
def serve_snapshot(request, alarm_id):
    """
    View для отображения изображения с использованием Bearer токена
    """
    try:
        alarm = Alarm.objects.get(alarm_id=alarm_id)
    except Alarm.DoesNotExist:
        raise Http404("Alarm not found")

    # Доступ к snapshot:
    # - staff/superuser: разрешаем всегда
    # - обычный пользователь: только если монитор разрешен через UserMonitorAccess
    user = getattr(request, 'user', None)
    _assert_events_access(user)
    if not (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
        monitor_pk = getattr(alarm, 'monitor_ref_id', None)
        if not monitor_pk:
            # best-effort: сопоставим по числовому Alarm.monitor_id
            try:
                m = Monitor.objects.filter(monitor_id=str(int(alarm.monitor_id))).only('id').first()
                monitor_pk = m.id if m else None
            except Exception:
                monitor_pk = None

        if not monitor_pk:
            raise Http404("Monitor not found")

        allowed = UserMonitorAccess.objects.filter(
            user_id=getattr(user, 'id', None),
            monitor_id=int(monitor_pk),
            enabled=True,
        ).exists()
        if not allowed:
            raise Http404("Forbidden")
    
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
