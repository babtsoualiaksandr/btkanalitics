import requests
import time
import os
from django.core.management.base import BaseCommand
from agromash.models import TelegramSubscriber

class Command(BaseCommand):
    help = 'Handle Telegram bot updates'

    def handle(self, *args, **options):
        token = os.environ.get('TLG_BOT_TOKEN')
        if not token:
            self.stdout.write(self.style.ERROR('TLG_BOT_TOKEN not set'))
            return

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
                                defaults={'username': username, 'subscribed_monitor_ids': []}
                            )
                            # Send welcome
                            requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                          json={'chat_id': chat_id, 'text': 'Welcome! You are subscribed.'})
                        offset = update['update_id'] + 1
                time.sleep(1)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error: {e}'))
                time.sleep(5)
