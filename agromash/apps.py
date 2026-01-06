from django.apps import AppConfig
import threading
import requests
import time
import sys
from django.conf import settings

from requests.exceptions import RequestException


import logging


logger = logging.getLogger(__name__)


class AgromashConfig(AppConfig):
    name = 'agromash'
    default_auto_field = 'django.db.models.BigAutoField'
    verbose_name = 'Настройки Парсера Аналитики'

    def ready(self):
        import agromash.signals
        from agromash.models import TelegramSubscriber, Monitor

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
                                if 'message' in update:
                                    chat_id = update['message']['chat']['id']
                                    text = update['message'].get('text', '')
                                    if text == '/start':
                                        username = update['message']['from'].get('username')
                                        sub, created = TelegramSubscriber.objects.get_or_create(
                                            chat_id=chat_id,
                                            defaults={'username': username, 'subscribed_monitor_ids': []}
                                        )
                                        if not created and username and sub.username != username:
                                            sub.username = username
                                            sub.save(update_fields=["username"])
                                        logger.info("/start subscriber=%s created=%s", chat_id, created)
                                        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                      json={'chat_id': chat_id, 'text': f'Welcome! {chat_id} You are subscribed. Hello'})
                                    elif text.startswith('/set '):
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
                                                requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                              json={'chat_id': chat_id, 'text': f'Monitor {monitor_id} added to your subscriptions.'})
                                            else:
                                                requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                              json={'chat_id': chat_id, 'text': f'Monitor {monitor_id} is already in your subscriptions.'})
                                        except (ValueError, IndexError):
                                            requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                         json={'chat_id': chat_id, 'text': 'Invalid command. Use /set <monitor_id> where monitor_id is a number.'})
                                offset = update['update_id'] + 1

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
