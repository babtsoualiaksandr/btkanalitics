import requests
import time
import os
import logging
from django.core.management.base import BaseCommand
from agromash.models import TelegramSubscriber
from django.conf import settings


logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Handle Telegram bot updates'

    def handle(self, *args, **options):
        token = os.environ.get('TLG_BOT_TOKEN')
        if not token:
            self.stdout.write(self.style.ERROR('TLG_BOT_TOKEN not set'))
            return

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

                            text = "Вы подписаны. Откройте Mini App для управления отчётами."
                            payload = {'chat_id': chat_id, 'text': text}
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
                        offset = update['update_id'] + 1
                time.sleep(1)
            except Exception:
                logger.exception("Telegram polling loop error")
                time.sleep(5)
