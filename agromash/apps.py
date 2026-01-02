from django.apps import AppConfig
import threading
import requests
import time
import sys
from django.conf import settings


class AgromashConfig(AppConfig):
    name = 'agromash'

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
                offset = 0
                while True:
                    try:
                        response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates?offset={offset}')
                        data = response.json()
                        if data.get('ok'):
                            for update in data['result']:
                                if 'message' in update:
                                    chat_id = update['message']['chat']['id']
                                    text = update['message'].get('text', '')
                                    if text == '/start':
                                        username = update['message']['from'].get('username')
                                        TelegramSubscriber.objects.get_or_create(
                                            chat_id=chat_id,
                                            defaults={'username': username, 'subscribed_monitor_ids': []}
                                        )
                                        requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                      json={'chat_id': chat_id, 'text': 'Welcome! You are subscribed.'})
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
                        time.sleep(1)
                    except Exception as e:
                        time.sleep(5)

        threading.Thread(target=run_bot, daemon=True).start()
