from django.apps import AppConfig
import threading
import requests
import time
from django.conf import settings


class AgromashConfig(AppConfig):
    name = 'agromash'

    def ready(self):
        import agromash.signals
        from agromash.models import TelegramSubscriber

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
                                if 'message' in update and update['message'].get('text') == '/start':
                                    chat_id = update['message']['chat']['id']
                                    username = update['message']['from'].get('username')
                                    TelegramSubscriber.objects.get_or_create(
                                        chat_id=chat_id,
                                        defaults={'username': username, 'subscribed_monitors': []}
                                    )
                                    requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                                  json={'chat_id': chat_id, 'text': 'Welcome! You are subscribed.'})
                                offset = update['update_id'] + 1
                        time.sleep(1)
                    except Exception as e:
                        time.sleep(5)

        threading.Thread(target=run_bot, daemon=True).start()
