from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Optional

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from agromash.models import Monitor, TelegramReportSubscription, TelegramSubscriber, TelegramSubscriberMonitorSubscription
from agromash.services.report_scheduler import compute_next_run_at
from agromash.services.telegram_webapp_auth import TelegramInitDataError, validate_webapp_init_data
from agromash.tasks import send_email_report_now, send_report_now, send_report_range_now


def _get_init_data(request: HttpRequest) -> str:
    # Telegram.WebApp.initData лучше всего передавать в заголовке, чтобы не светить в URL.
    raw = (
        request.headers.get("X-Telegram-Init-Data")
        or request.headers.get("X_Telegram_Init_Data")
        or request.POST.get("initData")
    )
    if raw:
        return str(raw)

    if request.body:
        try:
            return str(json.loads(request.body).get("initData") or "")
        except Exception:
            return ""
    return ""


def _authed_subscriber(request: HttpRequest) -> TelegramSubscriber:
    init_data = _get_init_data(request)
    parsed = validate_webapp_init_data(
        init_data=init_data,
        bot_token=str(getattr(settings, "TLG_BOT_TOKEN", "") or ""),
    )
    if not parsed.user:
        raise TelegramInitDataError("user missing")

    sub, created = TelegramSubscriber.objects.get_or_create(
        chat_id=int(parsed.user.id),
        defaults={"username": parsed.user.username or None, "subscribed_monitor_ids": []},
    )
    if not created and parsed.user.username and sub.username != parsed.user.username:
        sub.username = parsed.user.username
        sub.save(update_fields=["username"])
    return sub


def _subscription_to_dict(sub: TelegramReportSubscription) -> Dict[str, Any]:
    monitors = list(sub.monitors.all().order_by("monitor_id"))
    return {
        "id": sub.id,
        "email": sub.email or "",
        "frequency": sub.frequency,
        "period_from_minutes": int(sub.period_from_minutes or 0),
        "period_to_minutes": int(sub.period_to_minutes or 0),
        "send_pdf": bool(sub.send_pdf),
        "send_xlsx": bool(sub.send_xlsx),
        "enabled": bool(sub.enabled),
        "last_sent_at": sub.last_sent_at.isoformat() if sub.last_sent_at else None,
        "next_run_at": sub.next_run_at.isoformat() if sub.next_run_at else None,
        "monitors": [
            {
                "id": m.id,
                "monitor_id": str(m.monitor_id),
                "monitor_name": str(m.monitor_name),
            }
            for m in monitors
        ],
    }


@require_GET
def tg_app(request: HttpRequest):
    """UI для Telegram Mini App.

    Аутентификация выполняется на API-эндпоинтах по initData.
    """

    return render(request, "agromash/tg_app.html")


@csrf_exempt
@require_GET
def tg_api_subscriptions(request: HttpRequest):
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    subs = (
        TelegramReportSubscription.objects.filter(subscriber=subscriber)
        .prefetch_related("monitors")
        .order_by("id")
    )
    return JsonResponse({"ok": True, "subscriptions": [_subscription_to_dict(s) for s in subs]})


def _alarm_monitor_to_dict(link: TelegramSubscriberMonitorSubscription) -> Dict[str, Any]:
    m = link.monitor
    return {
        "monitor_pk": m.id,
        "monitor_id": str(m.monitor_id),
        "monitor_name": str(m.monitor_name),
        "enabled": bool(link.enabled),
    }


@csrf_exempt
@require_GET
def tg_api_alarm_monitors(request: HttpRequest):
    """Список мониторов, которые админ разрешил подписчику + enabled-флаг пользователя."""
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    links = (
        TelegramSubscriberMonitorSubscription.objects.filter(subscriber=subscriber)
        .select_related("monitor")
        .order_by("monitor__monitor_id")
    )
    return JsonResponse({"ok": True, "monitors": [_alarm_monitor_to_dict(x) for x in links]})


@csrf_exempt
@require_POST
def tg_api_alarm_monitor_set_enabled(request: HttpRequest, monitor_pk: int):
    """Вкл/выкл оповещения по Alarm для одного разрешённого монитора."""
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except Exception:
        payload = {}

    b = _parse_bool(payload.get("enabled"))
    if b is None:
        return JsonResponse({"ok": False, "error": "invalid enabled"}, status=400)

    # Важно: нельзя добавлять/удалять мониторы — только переключать уже назначенные.
    updated = TelegramSubscriberMonitorSubscription.objects.filter(
        subscriber=subscriber,
        monitor_id=int(monitor_pk),
    ).update(enabled=b)

    if not updated:
        # либо монитор не назначен админом, либо неверный pk
        return JsonResponse({"ok": False, "error": "monitor is not allowed"}, status=404)

    link = (
        TelegramSubscriberMonitorSubscription.objects.filter(subscriber=subscriber, monitor_id=int(monitor_pk))
        .select_related("monitor")
        .first()
    )
    return JsonResponse({"ok": True, "monitor": _alarm_monitor_to_dict(link) if link else None})


def _parse_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


@csrf_exempt
@require_POST
def tg_api_update_subscription(request: HttpRequest, subscription_id: int):
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except Exception:
        payload = {}

    sub = (
        TelegramReportSubscription.objects.filter(pk=int(subscription_id), subscriber=subscriber)
        .prefetch_related("monitors")
        .first()
    )
    if not sub:
        return JsonResponse({"ok": False, "error": "not found"}, status=404)

    allowed_freq = {c[0] for c in TelegramReportSubscription.FREQUENCY_CHOICES}
    update_fields = []

    if "email" in payload:
        email = (payload.get("email") or "").strip()
        sub.email = email or None
        update_fields.append("email")

    if "frequency" in payload:
        freq = str(payload.get("frequency") or "").strip()
        if freq and freq not in allowed_freq:
            return JsonResponse({"ok": False, "error": "invalid frequency"}, status=400)
        sub.frequency = freq or sub.frequency
        update_fields.append("frequency")

    if "period_from_minutes" in payload:
        try:
            sub.period_from_minutes = int(payload.get("period_from_minutes"))
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid period_from_minutes"}, status=400)
        if sub.period_from_minutes < 0:
            return JsonResponse({"ok": False, "error": "period_from_minutes must be >= 0"}, status=400)
        update_fields.append("period_from_minutes")

    if "period_to_minutes" in payload:
        try:
            sub.period_to_minutes = int(payload.get("period_to_minutes"))
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid period_to_minutes"}, status=400)
        if sub.period_to_minutes < 0:
            return JsonResponse({"ok": False, "error": "period_to_minutes must be >= 0"}, status=400)
        update_fields.append("period_to_minutes")

    if "send_pdf" in payload:
        b = _parse_bool(payload.get("send_pdf"))
        if b is None:
            return JsonResponse({"ok": False, "error": "invalid send_pdf"}, status=400)
        sub.send_pdf = b
        update_fields.append("send_pdf")

    if "send_xlsx" in payload:
        b = _parse_bool(payload.get("send_xlsx"))
        if b is None:
            return JsonResponse({"ok": False, "error": "invalid send_xlsx"}, status=400)
        sub.send_xlsx = b
        update_fields.append("send_xlsx")

    enabled_changed = False
    if "enabled" in payload:
        b = _parse_bool(payload.get("enabled"))
        if b is None:
            return JsonResponse({"ok": False, "error": "invalid enabled"}, status=400)
        enabled_changed = (sub.enabled != b)
        sub.enabled = b
        update_fields.append("enabled")

    # Если включили/изменили частоту — пересчитаем next_run_at (чтобы расписание не висело в прошлом)
    if sub.enabled and (enabled_changed or ("frequency" in payload)):
        now = timezone.now()
        sub.next_run_at = compute_next_run_at(now=now, frequency=sub.frequency)
        update_fields.append("next_run_at")

    if not sub.enabled and enabled_changed:
        sub.next_run_at = None
        update_fields.append("next_run_at")

    if update_fields:
        update_fields.append("updated_at")
        sub.save(update_fields=list(dict.fromkeys(update_fields)))

    return JsonResponse({"ok": True, "subscription": _subscription_to_dict(sub)})


@csrf_exempt
@require_POST
def tg_api_send_now(request: HttpRequest, subscription_id: int):
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except Exception:
        payload = {}

    channel = str(payload.get("channel") or "telegram").strip().lower()
    if channel not in ("telegram", "email", "both"):
        return JsonResponse({"ok": False, "error": "invalid channel"}, status=400)

    sub = TelegramReportSubscription.objects.filter(pk=int(subscription_id), subscriber=subscriber).first()
    if not sub:
        return JsonResponse({"ok": False, "error": "not found"}, status=404)

    task_ids = []
    if channel in ("telegram", "both"):
        async_res = send_report_now.delay(sub.id, source="tg_webapp")
        task_ids.append(async_res.id)
    if channel in ("email", "both"):
        if not sub.email:
            return JsonResponse({"ok": False, "error": "email is empty"}, status=400)
        async_res = send_email_report_now.delay(sub.id, source="tg_webapp")
        task_ids.append(async_res.id)

    return JsonResponse({"ok": True, "task_ids": task_ids})


def _parse_iso_dt(s: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        raise ValueError("invalid datetime")


@csrf_exempt
@require_POST
def tg_api_send_range(request: HttpRequest, subscription_id: int):
    try:
        subscriber = _authed_subscriber(request)
    except TelegramInitDataError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except Exception:
        payload = {}

    channel = str(payload.get("channel") or "telegram").strip().lower()
    if channel not in ("telegram", "email", "both"):
        return JsonResponse({"ok": False, "error": "invalid channel"}, status=400)

    start_s = str(payload.get("start") or "").strip()
    end_s = str(payload.get("end") or "").strip()
    if not start_s or not end_s:
        return JsonResponse({"ok": False, "error": "start/end required"}, status=400)

    # Валидируем ISO на входе, чтобы возвращать понятную ошибку ещё до Celery.
    try:
        _parse_iso_dt(start_s)
        _parse_iso_dt(end_s)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    sub = TelegramReportSubscription.objects.filter(pk=int(subscription_id), subscriber=subscriber).first()
    if not sub:
        return JsonResponse({"ok": False, "error": "not found"}, status=404)

    task_ids = []
    if channel in ("telegram", "both"):
        async_res = send_report_range_now.delay(sub.id, start_s, end_s, source="tg_webapp")
        task_ids.append(async_res.id)
    if channel in ("email", "both"):
        if not sub.email:
            return JsonResponse({"ok": False, "error": "email is empty"}, status=400)
        from agromash.tasks import send_email_report_range_now

        async_res = send_email_report_range_now.delay(sub.id, start_s, end_s, source="tg_webapp")
        task_ids.append(async_res.id)

    return JsonResponse({"ok": True, "task_ids": task_ids})
