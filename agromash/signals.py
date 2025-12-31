from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Alarm, TelegramSubscriber
import requests
from django.conf import settings

@receiver(post_save, sender=Alarm)
def send_alarm_to_telegram(sender, instance, created, **kwargs):
    if created and instance.original_quality_snapshot:
        print("я тут")
        token = settings.TLG_BOT_TOKEN
        chat_id = settings.TLG_CHAT_ID
        base_url = settings.BASE_URL
        print(f"{token} and {chat_id} and {base_url} and {instance.account.access_token} _")
        if token and chat_id and base_url and instance.account.access_token:
            try:
                image_url = base_url + instance.original_quality_snapshot
                print(image_url)
                headers = {'Authorization': f'Bearer {instance.account.access_token}'}
                image_response = requests.get(image_url, headers=headers)
                print(image_response)
                if image_response.status_code == 200:
                    telegram_url = f'https://api.telegram.org/bot{token}/sendPhoto'
                    files = {'photo': ('snapshot.jpg', image_response.content, 'image/jpeg')}
                    chat_ids = TelegramSubscriber.objects.values_list("chat_id", flat=True)
                    chat_ids_list = list(chat_ids)
                    for chat_id in chat_ids_list:
                        data = {'chat_id': chat_id, 'caption': f'New alarm: {instance.topic} for monitor {instance.monitor_name}'}
                        requests.post(telegram_url, data=data, files=files)
                    print("*****************")
            except Exception as e:
                print(e)
