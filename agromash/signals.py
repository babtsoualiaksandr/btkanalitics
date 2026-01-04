from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver
from django.db.models import Q

from .models import Alarm, Monitor, TelegramSubscriber
from .services.alarm_data_parser import format_alarm_caption, parse_alarm_data
from django.conf import settings

import logging

from .va_api_client import VAApiClient
from .services.telegram_client import send_message, send_photo


logger = logging.getLogger(__name__)


def _telegram_send_message(*, chat_id: int, text: str) -> None:
    """Безопасная отправка сообщения в Telegram."""
    try:
        send_message(chat_id=chat_id, text=text, meta={"source": "signals"})
    except Exception:
        logger.exception("Ошибка отправки сообщения в Telegram (chat_id=%s)", chat_id)


def _get_alarm_subscribers_chat_ids(instance: Alarm) -> list[int]:
    """Вернуть список chat_id подписчиков, подписанных на монитор алерта.

    Поддерживает:
      - актуальную ManyToMany-связь TelegramSubscriber.subscribed_monitors
      - legacy JSONField TelegramSubscriber.subscribed_monitor_ids (как список str/int)
    """

    monitor_id = getattr(instance, "monitor_id", None)
    monitor_id_str = "" if monitor_id is None else str(monitor_id)

    subscribers_qs = TelegramSubscriber.objects.filter(
        Q(subscribed_monitors__monitor_id=monitor_id_str)
        # fallback: legacy JSON (если есть старые данные)
        | Q(subscribed_monitor_ids__contains=[monitor_id_str])
        | Q(subscribed_monitor_ids__contains=[monitor_id])
    ).distinct()

    return list(subscribers_qs.values_list("chat_id", flat=True))

@receiver(post_save, sender=Alarm)
def send_alarm_to_telegram(sender, instance, created, **kwargs):
    if created and instance.original_quality_snapshot:
        token = getattr(settings, "TLG_BOT_TOKEN", None)
        base_url = getattr(settings, "BASE_URL", None)
        if token and base_url and instance.account_id:
            try:
                client = VAApiClient(account_id=instance.account_id, base_url=base_url)
                img_resp = client.request('GET', instance.original_quality_snapshot)
                try:
                    if img_resp.status_code == 200:
                        # Формируем caption из распарсенного Alarm.data (topic-зависимая логика).
                        try:
                            parsed_alarm = parse_alarm_data(instance.data or {})
                            caption = format_alarm_caption(parsed_alarm)
                        except Exception:
                            logger.exception(
                                "Не удалось распарсить Alarm.data для caption (alarm_id=%s)",
                                instance.alarm_id,
                            )
                            caption = f"New alarm: {instance.topic}"

                        # Шлем только тем подписчикам, кто подписан на монитор алерта.
                        # (Ранее отправлялось всем, игнорируя настройки подписок.)
                        chat_ids_list = _get_alarm_subscribers_chat_ids(instance)
                        for chat_id in chat_ids_list:
                            send_photo(
                                chat_id=chat_id,
                                filename="snapshot.jpg",
                                content=img_resp.content,
                                mime_type="image/jpeg",
                                caption=caption,
                                alarm=instance,
                                meta={
                                    "source": "alarm_signal",
                                    "alarm_id": instance.alarm_id,
                                    "topic": instance.topic,
                                },
                            )
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


@receiver(m2m_changed, sender=TelegramSubscriber.subscribed_monitors.through)
def notify_subscriber_monitors_changed(sender, instance: TelegramSubscriber, action: str, pk_set, **kwargs):
    """Уведомлять подписчика при изменении списка мониторов (в т.ч. из админки)."""

    if action not in ("post_add", "post_remove", "post_clear"):
        return

    if not instance or not getattr(instance, "chat_id", None):
        return

    if action == "post_clear":
        _telegram_send_message(chat_id=instance.chat_id, text="Вы отписаны от всех мониторов.")
        return

    monitor_ids = list(pk_set or [])
    if not monitor_ids:
        return

    monitors = list(Monitor.objects.filter(pk__in=monitor_ids).order_by("monitor_id"))
    if not monitors:
        return

    lines = [f"• {m.monitor_name} (ID: {m.monitor_id})" for m in monitors]
    if action == "post_add":
        _telegram_send_message(chat_id=instance.chat_id, text="Вы подписаны на мониторы:\n" + "\n".join(lines))
    elif action == "post_remove":
        _telegram_send_message(chat_id=instance.chat_id, text="Вы отписаны от мониторов:\n" + "\n".join(lines))
