from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Alarm, TelegramSubscriber
import requests
from django.conf import settings

import logging

from .va_api_client import VAApiClient


logger = logging.getLogger(__name__)

@receiver(post_save, sender=Alarm)
def send_alarm_to_telegram(sender, instance, created, **kwargs):
    if created and instance.original_quality_snapshot:
        token = settings.TLG_BOT_TOKEN
        base_url = settings.BASE_URL
        if token and base_url and instance.account_id:
            try:
                client = VAApiClient(account_id=instance.account_id, base_url=base_url)
                img_resp = client.request('GET', instance.original_quality_snapshot)
                try:
                    if img_resp.status_code == 200:
                        telegram_url = f'https://api.telegram.org/bot{token}/sendPhoto'
                        files = {'photo': ('snapshot.jpg', img_resp.content, 'image/jpeg')}
                        chat_ids = TelegramSubscriber.objects.values_list("chat_id", flat=True)
                        chat_ids_list = list(chat_ids)
                        for chat_id in chat_ids_list:
                            data = {'chat_id': chat_id, 'caption': f'New alarm: {instance.topic} for monitor {instance.monitor_name}'}
                            requests.post(telegram_url, data=data, files=files)
                        logger.info(
                            "Alarm отправлен в Telegram (alarm_id=%s, subscribers=%s)",
                            instance.alarm_id,
                            len(chat_ids_list),
                        )
                    else:
                        logger.warning(
                            "Не удалось получить snapshot (status=%s, alarm_id=%s)",
                            img_resp.status_code,
                            instance.alarm_id,
                        )
                finally:
                    img_resp.close()
            except Exception as e:
                logger.exception(
                    "Ошибка отправки alarm в Telegram (alarm_id=%s): %s",
                    instance.alarm_id,
                    e,
                )
