from django.apps import AppConfig
import threading
import requests
import time
import sys
import os
from django.conf import settings
from django.utils import timezone

from requests.exceptions import RequestException


import logging


logger = logging.getLogger(__name__)


class AgromashConfig(AppConfig):
    name = 'agromash'
    default_auto_field = 'django.db.models.BigAutoField'
    verbose_name = 'Настройки Парсера Аналитики'

    def ready(self):
        import agromash.signals
        from agromash.models import TelegramSubscriber, Monitor, AccountVideoAnalytics
        from agromash.tasks import parse_event_task, request_stop_parser

        # Не запускаем Telegram polling в Celery worker/beat и других management commands.
        # Иначе каждый воркер поднимет собственный бесконечный поток.
        if any(arg in sys.argv for arg in ("celery", "worker", "beat")):
            return
        if "runserver" not in sys.argv:
            return

        def run_bot():
            token = settings.TLG_BOT_TOKEN
            if token:
                session = requests.Session()

                # URL Mini App (можно переопределить переменной окружения)
                base_url = str(getattr(settings, "BASE_URL", "") or "").rstrip("/")
                webapp_url = (
                    os.environ.get("TLG_WEBAPP_URL")
                    or getattr(settings, "TLG_WEBAPP_URL", None)
                    or (f"{base_url}/agromash/tg/" if base_url else "")
                )

                def _admin_chat_ids() -> set[int]:
                    raw = getattr(settings, "TLG_CHAT_ID_ADMINS", None) or []
                    out: set[int] = set()
                    for x in raw:
                        try:
                            out.add(int(str(x).strip()))
                        except Exception:
                            continue
                    return out

                def _is_admin(chat_id: int) -> bool:
                    return int(chat_id) in _admin_chat_ids()

                def _tg_post(method: str, payload: dict) -> None:
                    try:
                        session.post(
                            f"https://api.telegram.org/bot{token}/{method}",
                            json=payload,
                            timeout=15,
                        )
                    except Exception:
                        logger.exception("Telegram API call failed method=%s", method)

                def _register_bot_commands() -> None:
                    """Записать команды в меню Telegram (Bot API setMyCommands).

                    - default scope: базовые команды для всех
                    - chat scope: расширенный набор для админов (TLG_CHAT_ID_ADMINS)
                    """

                    base_commands = [
                        {"command": "start", "description": "Старт / регистрация"},
                        {"command": "app", "description": "Открыть Mini App"},
                        {"command": "set", "description": "Подписаться на monitor: /set <id>"},
                    ]
                    admin_extra = [
                        {"command": "parsers", "description": "Статус парсеров (админ)"},
                        {"command": "va", "description": "Alias /parsers (админ)"},
                        {"command": "parser", "description": "Упр. парсером: /parser <id> start|stop (админ)"},
                    ]

                    # default
                    _tg_post(
                        "setMyCommands",
                        {
                            "commands": base_commands,
                            "scope": {"type": "default"},
                        },
                    )

                    # admins
                    for cid in sorted(_admin_chat_ids()):
                        _tg_post(
                            "setMyCommands",
                            {
                                "commands": base_commands + admin_extra,
                                "scope": {"type": "chat", "chat_id": int(cid)},
                            },
                        )

                def _fmt_age(dt) -> str:
                    if not dt:
                        return "-"
                    sec = int((timezone.now() - dt).total_seconds())
                    if sec < 0:
                        sec = 0
                    if sec < 60:
                        return f"{sec}s"
                    mins = sec // 60
                    if mins < 60:
                        return f"{mins}m"
                    hrs = mins // 60
                    return f"{hrs}h"

                def _heart_icon(acc: AccountVideoAnalytics) -> str:
                    # Пользователь просил «сердце бьётся / нет».
                    if acc.is_parser_running:
                        return "💓"
                    if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_RUNNING:
                        # running, но heartbeat протух
                        return "💔"
                    if acc.parser_status in (
                        AccountVideoAnalytics.PARSER_STATUS_STARTING,
                        AccountVideoAnalytics.PARSER_STATUS_STOPPING,
                    ):
                        return "⏳"
                    if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_ERROR:
                        return "⚠️"
                    return "⏹"

                def _parsers_text() -> str:
                    accounts = list(AccountVideoAnalytics.objects.all().order_by("id"))
                    lines = [
                        "Parser Status (VA accounts)",
                        "Легенда: 💓 online | 💔 нет heartbeat | ⏹ stopped | ⏳ переход | ⚠️ error",
                        "",
                    ]
                    for a in accounts:
                        hb = _fmt_age(getattr(a, "parser_heartbeat_at", None))
                        task = (getattr(a, "parser_task_id", None) or "-")
                        status = getattr(a, "parser_status", "-")
                        lines.append(
                            f"{_heart_icon(a)} #{a.id} {a.name} | {a.organization} | {status} | hb={hb} | task={task}"
                        )
                        if getattr(a, "parser_last_error", None) and status == AccountVideoAnalytics.PARSER_STATUS_ERROR:
                            err = str(a.parser_last_error).strip().splitlines()[0][:120]
                            lines.append(f"    error: {err}")
                    if len(accounts) == 0:
                        lines.append("Нет AccountVideoAnalytics")
                    return "\n".join(lines)

                def _parsers_keyboard() -> dict:
                    rows = []
                    # refresh
                    rows.append([
                        {"text": "🔄 Refresh", "callback_data": "va:refresh"},
                    ])

                    for a in AccountVideoAnalytics.objects.all().order_by("id"):
                        if a.is_parser_running:
                            rows.append([
                                {"text": f"Stop #{a.id}", "callback_data": f"va:stop:{a.id}"},
                            ])
                        else:
                            rows.append([
                                {"text": f"Start #{a.id}", "callback_data": f"va:start:{a.id}"},
                            ])
                    return {"inline_keyboard": rows}

                def _admin_send_parsers(chat_id: int) -> None:
                    _tg_post(
                        "sendMessage",
                        {
                            "chat_id": int(chat_id),
                            "text": _parsers_text(),
                            "reply_markup": _parsers_keyboard(),
                        },
                    )

                def _admin_toggle(*, action: str, account_id: int) -> str:
                    acc = AccountVideoAnalytics.objects.filter(pk=int(account_id)).first()
                    if not acc:
                        return f"AccountVideoAnalytics id={account_id} не найден"

                    if action == "start":
                        if acc.is_parser_running:
                            return f"#{acc.id} уже запущен"
                        async_res = parse_event_task.delay(acc.id)
                        AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
                            parser_task_id=async_res.id,
                            parser_stop_requested=False,
                            parser_last_error=None,
                        )
                        return f"#{acc.id} старт поставлен в очередь (task_id={async_res.id})"

                    if action == "stop":
                        if not acc.is_parser_running and acc.parser_status != AccountVideoAnalytics.PARSER_STATUS_RUNNING:
                            return f"#{acc.id} не запущен"
                        task_id = request_stop_parser(account_id=acc.id, terminate=True)
                        return f"#{acc.id} остановка запрошена" + (f" (task_id={task_id})" if task_id else "")

                    return "invalid action"

                # Если был настроен webhook, polling через getUpdates не будет работать.
                # Для этого проекта используем polling, поэтому перед стартом снимаем webhook.
                try:
                    session.get(
                        f"https://api.telegram.org/bot{token}/deleteWebhook",
                        params={"drop_pending_updates": True},
                        timeout=15,
                    )
                except RequestException as e:
                    # Сетевые ошибки бывают временными (блокировки/обрывы/перезапуск сети).
                    # Не спамим stacktrace в journalctl.
                    logger.warning("Failed to deleteWebhook (polling may not work): %s", e)
                except Exception:
                    logger.exception("Failed to deleteWebhook (polling may not work)")

                # Обновляем меню команд (best-effort)
                try:
                    _register_bot_commands()
                except Exception:
                    logger.exception("Failed to register bot commands")

                offset = 0
                backoff_sec = 1
                while True:
                    try:
                        response = session.get(
                            f'https://api.telegram.org/bot{token}/getUpdates?offset={offset}',
                            timeout=30,
                        )
                        data = response.json()
                        if data.get('ok'):
                            for update in data['result']:
                                # Важно: offset обновляем сразу, иначе при `continue` получим дубль-обработку.
                                offset = update['update_id'] + 1

                                if 'message' in update:
                                    chat_id = int(update['message']['chat']['id'])
                                    text = update['message'].get('text', '') or ''
                                    text = str(text).strip()

                                    # --- base commands (для всех) ---
                                    if text in ('/start', '/app'):
                                        username = update['message'].get('from', {}).get('username')
                                        sub, created = TelegramSubscriber.objects.get_or_create(
                                            chat_id=chat_id,
                                            defaults={'username': username, 'subscribed_monitor_ids': []}
                                        )
                                        if not created and username and sub.username != username:
                                            sub.username = username
                                            sub.save(update_fields=["username"])
                                        logger.info("/start subscriber=%s created=%s", chat_id, created)

                                        payload = {
                                            "chat_id": chat_id,
                                            "text": f"Вы подписаны. chat_id={chat_id}\nКоманды: /set <monitor_id>\nДля отчётов используйте Mini App.",
                                        }
                                        if webapp_url:
                                            payload["reply_markup"] = {
                                                "inline_keyboard": [[
                                                    {"text": "Открыть отчёты", "web_app": {"url": webapp_url}},
                                                ]],
                                            }
                                        _tg_post("sendMessage", payload)
                                        # offset уже обновили выше
                                        continue

                                    # --- admin commands ---
                                    if text.startswith('/parsers') or text.startswith('/va'):
                                        if not _is_admin(chat_id):
                                            _tg_post(
                                                "sendMessage",
                                                {"chat_id": chat_id, "text": "Доступ запрещён"},
                                            )
                                            continue
                                        _admin_send_parsers(chat_id)
                                        continue

                                    if text.startswith('/parser'):
                                        if not _is_admin(chat_id):
                                            _tg_post(
                                                "sendMessage",
                                                {"chat_id": chat_id, "text": "Доступ запрещён"},
                                            )
                                            continue
                                        parts = text.split()
                                        if len(parts) < 3:
                                            _tg_post(
                                                "sendMessage",
                                                {
                                                    "chat_id": chat_id,
                                                    "text": "Использование: /parser <id> start|stop",
                                                },
                                            )
                                            continue
                                        try:
                                            account_id = int(parts[1])
                                        except Exception:
                                            _tg_post(
                                                "sendMessage",
                                                {"chat_id": chat_id, "text": "id должен быть числом"},
                                            )
                                            continue
                                        action = str(parts[2]).strip().lower()
                                        action = "start" if action in ("start", "on", "run") else action
                                        action = "stop" if action in ("stop", "off") else action
                                        msg = _admin_toggle(action=action, account_id=account_id)
                                        _tg_post(
                                            "sendMessage",
                                            {"chat_id": chat_id, "text": msg},
                                        )
                                        continue

                                    # --- legacy: /set monitor ---
                                    if text.startswith('/set '):
                                        try:
                                            monitor_id = int(text.split()[1])
                                            subscriber, created = TelegramSubscriber.objects.get_or_create(
                                                chat_id=chat_id,
                                                defaults={'username': update['message']['from'].get('username'), 'subscribed_monitor_ids': []}
                                            )
                                            monitor_obj, _ = Monitor.objects.get_or_create(
                                                monitor_id=str(monitor_id),
                                                defaults={
                                                    'monitor_name': str(monitor_id),
                                                    'topic': '',
                                                },
                                            )

                                            if not subscriber.subscribed_monitors.filter(pk=monitor_obj.pk).exists():
                                                subscriber.subscribed_monitors.add(monitor_obj)
                                                _tg_post(
                                                    "sendMessage",
                                                    {"chat_id": chat_id, "text": f"Monitor {monitor_id} added to your subscriptions."},
                                                )
                                            else:
                                                _tg_post(
                                                    "sendMessage",
                                                    {"chat_id": chat_id, "text": f"Monitor {monitor_id} is already in your subscriptions."},
                                                )
                                        except (ValueError, IndexError):
                                            _tg_post(
                                                "sendMessage",
                                                {"chat_id": chat_id, "text": "Invalid command. Use /set <monitor_id>"},
                                            )

                                if 'callback_query' in update:
                                    cb = update.get('callback_query') or {}
                                    from_user = cb.get('from') or {}
                                    from_id = int(from_user.get('id') or 0)
                                    data_s = str(cb.get('data') or "")
                                    cb_id = cb.get('id')
                                    msg_obj = cb.get('message') or {}
                                    chat = msg_obj.get('chat') or {}
                                    chat_id = int(chat.get('id') or 0)
                                    message_id = msg_obj.get('message_id')

                                    if cb_id:
                                        _tg_post(
                                            "answerCallbackQuery",
                                            {"callback_query_id": cb_id},
                                        )

                                    if not _is_admin(from_id) and not _is_admin(chat_id):
                                        _tg_post(
                                            "sendMessage",
                                            {"chat_id": chat_id, "text": "Доступ запрещён"},
                                        )
                                    else:
                                        # va:refresh | va:start:<id> | va:stop:<id>
                                        parts = data_s.split(":")
                                        if len(parts) >= 2 and parts[0] == "va":
                                            cmd = parts[1]
                                            if cmd == "refresh" and chat_id and message_id:
                                                _tg_post(
                                                    "editMessageText",
                                                    {
                                                        "chat_id": chat_id,
                                                        "message_id": message_id,
                                                        "text": _parsers_text(),
                                                        "reply_markup": _parsers_keyboard(),
                                                    },
                                                )
                                            elif cmd in ("start", "stop") and len(parts) >= 3:
                                                try:
                                                    account_id = int(parts[2])
                                                except Exception:
                                                    account_id = 0
                                                res_text = _admin_toggle(action=cmd, account_id=account_id)
                                                if chat_id and message_id:
                                                    # Обновим текст/кнопки + отправим отдельное подтверждение
                                                    _tg_post(
                                                        "editMessageText",
                                                        {
                                                            "chat_id": chat_id,
                                                            "message_id": message_id,
                                                            "text": _parsers_text(),
                                                            "reply_markup": _parsers_keyboard(),
                                                        },
                                                    )
                                                if chat_id:
                                                    _tg_post(
                                                        "sendMessage",
                                                        {"chat_id": chat_id, "text": res_text},
                                                    )


                        # Успешный цикл — сброс backoff
                        backoff_sec = 1
                        time.sleep(1)
                    except RequestException as e:
                        # Типовая ситуация: Connection reset by peer / TLS handshake etc.
                        # Логируем без traceback и применяем backoff.
                        logger.warning("Telegram polling network error: %s", e)
                        time.sleep(backoff_sec)
                        backoff_sec = min(60, backoff_sec * 2)
                    except Exception:
                        logger.exception("Telegram polling loop error")
                        time.sleep(5)

        threading.Thread(target=run_bot, daemon=True).start()
