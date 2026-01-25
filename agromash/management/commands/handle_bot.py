import html
import requests
import time
import os
import logging
from django.core.management.base import BaseCommand
from agromash.models import TelegramSubscriber, AccountVideoAnalytics
from django.conf import settings
from django.utils import timezone

from agromash.tasks import parse_event_task, request_stop_parser


logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Handle Telegram bot updates'

    def handle(self, *args, **options):
        token = os.environ.get('TLG_BOT_TOKEN')
        if not token:
            self.stdout.write(self.style.ERROR('TLG_BOT_TOKEN not set'))
            return

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
            if acc.is_parser_running:
                return "💓"
            if acc.parser_status == AccountVideoAnalytics.PARSER_STATUS_RUNNING:
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
                "<b>Parser Status (VA accounts)</b>",
                "<i>Легенда:</i> 💓 online | 💔 нет heartbeat | ⏹ stopped | ⏳ переход | ⚠️ error",
                "",
            ]
            for a in accounts:
                hb = _fmt_age(getattr(a, "parser_heartbeat_at", None))
                task = (getattr(a, "parser_task_id", None) or "-")
                status = getattr(a, "parser_status", "-")
                h = html.escape
                lines.append(
                    (
                        f"{_heart_icon(a)} <code>#{h(str(a.id))}</code> "
                        f"{h(str(a.name or ''))} | {h(str(a.organization or ''))} | "
                        f"<b>{h(str(status))}</b> | hb=<code>{h(str(hb))}</code> | task=<code>{h(str(task))}</code>"
                    )
                )
            if len(accounts) == 0:
                lines.append("<i>Нет AccountVideoAnalytics</i>")
            lines.append("\nКоманды: <code>/parser &lt;id&gt; start|stop</code>")
            return "\n".join(lines)

        def _parsers_keyboard() -> dict:
            # Telegram inline keyboard не поддерживает цвета кнопок.
            # В качестве визуального различия используем emoji:
            #   🔴 stop / 🟢 start / 🟠 restart
            rows = [[{"text": "🔄 Refresh", "callback_data": "va:refresh"}]]
            for a in AccountVideoAnalytics.objects.all().order_by("id"):
                if a.is_parser_running:
                    rows.append([
                        {"text": f"🔴 Stop #{a.id}", "callback_data": f"va:stop:{a.id}"},
                    ])
                else:
                    label = (
                        f"🟠 Restart #{a.id}"
                        if a.parser_status == AccountVideoAnalytics.PARSER_STATUS_RUNNING
                        else f"🟢 Start #{a.id}"
                    )
                    rows.append([
                        {"text": label, "callback_data": f"va:start:{a.id}"},
                    ])
            return {"inline_keyboard": rows}

        def _tg_post(method: str, payload: dict) -> None:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    json=payload,
                    timeout=15,
                )
            except Exception:
                logger.exception("Telegram API call failed method=%s", method)

        def _register_bot_commands() -> None:
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

            _tg_post(
                "setMyCommands",
                {
                    "commands": base_commands,
                    "scope": {"type": "default"},
                },
            )
            for cid in sorted(_admin_chat_ids()):
                _tg_post(
                    "setMyCommands",
                    {
                        "commands": base_commands + admin_extra,
                        "scope": {"type": "chat", "chat_id": int(cid)},
                    },
                )

        # URL Mini App (можно переопределить переменной окружения)
        base_url = str(getattr(settings, "BASE_URL", "") or "").rstrip("/")
        webapp_url = (
            os.environ.get("TLG_WEBAPP_URL")
            or getattr(settings, "TLG_WEBAPP_URL", None)
            or (f"{base_url}/agromash/tg/" if base_url else "")
        )

        # Для polling через getUpdates webhook должен быть выключен.
        try:
            requests.get(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                params={"drop_pending_updates": True},
                timeout=15,
            )
        except Exception:
            logger.exception("Failed to deleteWebhook (polling may not work)")

        # Обновляем меню команд (best-effort)
        try:
            _register_bot_commands()
        except Exception:
            logger.exception("Failed to register bot commands")

        offset = 0
        while True:
            try:
                response = requests.get(
                    f'https://api.telegram.org/bot{token}/getUpdates?offset={offset}',
                    timeout=30,
                )
                data = response.json()
                if data.get('ok'):
                    for update in data['result']:
                        offset = update['update_id'] + 1
                        if 'message' in update and update['message'].get('text') in ('/start', '/app'):
                            chat_id = update['message']['chat']['id']
                            username = update['message']['from'].get('username')
                            sub, created = TelegramSubscriber.objects.get_or_create(
                                chat_id=chat_id,
                                defaults={'username': username, 'subscribed_monitor_ids': []}
                            )
                            if not created and username and sub.username != username:
                                sub.username = username
                                sub.save(update_fields=["username"])
                            logger.info("/start subscriber=%s created=%s", chat_id, created)

                            text = (
                                "<b>Вы подписаны</b>\n"
                                f"<code>chat_id={html.escape(str(chat_id))}</code>\n"
                                "Для управления отчётами используйте Mini App."
                            )
                            payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
                            if webapp_url:
                                payload['reply_markup'] = {
                                    'inline_keyboard': [[{
                                        'text': 'Открыть отчёты',
                                        'web_app': {'url': webapp_url},
                                    }]],
                                }
                            requests.post(
                                f'https://api.telegram.org/bot{token}/sendMessage',
                                json=payload,
                                timeout=15,
                            )

                        if 'message' in update and update['message'].get('text'):
                            chat_id = int(update['message']['chat']['id'])
                            text = str(update['message'].get('text') or '').strip()

                            if text.startswith('/parsers') or text.startswith('/va'):
                                if not _is_admin(chat_id):
                                    requests.post(
                                        f'https://api.telegram.org/bot{token}/sendMessage',
                                        json={'chat_id': chat_id, 'text': '⛔ <b>Доступ запрещён</b>', 'parse_mode': 'HTML'},
                                        timeout=15,
                                    )
                                    continue

                                requests.post(
                                    f'https://api.telegram.org/bot{token}/sendMessage',
                                    json={
                                        'chat_id': chat_id,
                                        'text': _parsers_text(),
                                        'parse_mode': 'HTML',
                                        'disable_web_page_preview': True,
                                        'reply_markup': _parsers_keyboard(),
                                    },
                                    timeout=15,
                                )
                                continue

                            if text.startswith('/parser'):
                                if not _is_admin(chat_id):
                                    requests.post(
                                        f'https://api.telegram.org/bot{token}/sendMessage',
                                        json={'chat_id': chat_id, 'text': '⛔ <b>Доступ запрещён</b>', 'parse_mode': 'HTML'},
                                        timeout=15,
                                    )
                                    continue

                                parts = text.split()
                                if len(parts) < 3:
                                    requests.post(
                                        f'https://api.telegram.org/bot{token}/sendMessage',
                                        json={'chat_id': chat_id, 'text': 'Использование: <code>/parser &lt;id&gt; start|stop</code>', 'parse_mode': 'HTML'},
                                        timeout=15,
                                    )
                                    continue

                                try:
                                    account_id = int(parts[1])
                                except Exception:
                                    requests.post(
                                        f'https://api.telegram.org/bot{token}/sendMessage',
                                        json={'chat_id': chat_id, 'text': 'id должен быть числом', 'parse_mode': 'HTML'},
                                        timeout=15,
                                    )
                                    continue

                                action = str(parts[2]).strip().lower()
                                action = 'start' if action in ('start', 'on', 'run') else action
                                action = 'stop' if action in ('stop', 'off') else action

                                acc = AccountVideoAnalytics.objects.filter(pk=account_id).first()
                                if not acc:
                                    msg = f'AccountVideoAnalytics id={account_id} не найден'
                                elif action == 'start':
                                    if acc.is_parser_running:
                                        msg = f'#{acc.id} уже запущен'
                                    else:
                                        async_res = parse_event_task.delay(acc.id)
                                        AccountVideoAnalytics.objects.filter(pk=acc.id).update(
                                            parser_status=AccountVideoAnalytics.PARSER_STATUS_STARTING,
                                            parser_task_id=async_res.id,
                                            parser_stop_requested=False,
                                            parser_last_error=None,
                                        )
                                        msg = f'#{acc.id} старт поставлен в очередь (task_id={async_res.id})'
                                elif action == 'stop':
                                    task_id = request_stop_parser(account_id=acc.id, terminate=True)
                                    msg = f'#{acc.id} остановка запрошена' + (f' (task_id={task_id})' if task_id else '')
                                else:
                                    msg = 'Использование: /parser <id> start|stop'

                                requests.post(
                                    f'https://api.telegram.org/bot{token}/sendMessage',
                                    json={'chat_id': chat_id, 'text': f"ℹ️ {html.escape(str(msg or ''))}", 'parse_mode': 'HTML'},
                                    timeout=15,
                                )
                time.sleep(1)
            except Exception:
                logger.exception("Telegram polling loop error")
                time.sleep(5)
