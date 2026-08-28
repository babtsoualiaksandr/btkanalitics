from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver
from django.db.models import Q
from django.db import IntegrityError, transaction

from .models import Alarm, Monitor, TelegramSubscriber, TelegramSubscriberMonitorSubscription, PlateIdentity
from .services.alarm_data_parser import format_alarm_caption, parse_alarm_data
from django.conf import settings

import logging
import html

from .va_api_client import VAApiClient
from .services.telegram_client import send_message, send_photo
from .services.plate_identities import extract_plate_rows


logger = logging.getLogger(__name__)


def _alarm_topic_icon(topic: str) -> str:
    """"Разноцветный" индикатор (реальных цветов в тексте Telegram нет).

    Telegram Bot API не поддерживает цвет текста/частей сообщения.
    Поэтому используем emoji-маркеры как визуальный аналог.
    """

    t = str(topic or "")
    return {
        "PlateMatched": "🟢",
        "PlateNotMatched": "🟠",
        "FaceNotMatched": "🔵",
        "LineCrossed": "🟡",
    }.get(t, "🔴")


def _trim_tg_caption(text: str, limit: int = 1024) -> str:
    """Обрезать подпись к фото под лимит Telegram."""

    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _format_alarm_caption_html(*, topic: str, caption_plain: str) -> str:
    """HTML-caption для Telegram (поддерживается в sendPhoto caption).

    Важно: Telegram не умеет цвета в тексте, поэтому "разноцветность" даём через emoji.
    """

    icon = _alarm_topic_icon(topic)
    # caption_plain у нас уже человекочитаемый (topic | monitor | channel | ...)
    # Переводим в многострочный вид для читабельности.
    parts = [p.strip() for p in str(caption_plain or "").split("|") if p.strip()]
    head = html.escape(parts[0]) if parts else html.escape(str(topic or "Alarm"))
    tail = parts[1:]

    lines: list[str] = [f"{icon} <b>{head}</b>"]
    for p in tail:
        lines.append(f"• {html.escape(p)}")
    return _trim_tg_caption("\n".join(lines))


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

    # 1) Актуальная схема: через through-модель с флагом enabled
    enabled_chat_ids = list(
        TelegramSubscriberMonitorSubscription.objects.filter(
            enabled=True,
            monitor__monitor_id=monitor_id_str,
        ).values_list("subscriber__chat_id", flat=True)
    )

    # 2) Legacy fallback: JSON список monitor_id (без возможности disable)
    legacy_chat_ids = list(
        TelegramSubscriber.objects.filter(
            Q(subscribed_monitor_ids__contains=[monitor_id_str])
            | Q(subscribed_monitor_ids__contains=[monitor_id])
        ).values_list("chat_id", flat=True)
    )

    # уникальные, стабильный порядок
    seen = set()
    out = []
    for cid in [*enabled_chat_ids, *legacy_chat_ids]:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(int(cid))
    return out

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

                        # "Разноцветный" caption для Telegram (реальные цвета в тексте не поддерживаются).
                        caption_html = _format_alarm_caption_html(topic=instance.topic, caption_plain=caption)

                        # Шлем только тем подписчикам, кто подписан на монитор алерта.
                        # (Ранее отправлялось всем, игнорируя настройки подписок.)
                        chat_ids_list = _get_alarm_subscribers_chat_ids(instance)
                        for chat_id in chat_ids_list:
                            send_photo(
                                chat_id=chat_id,
                                filename="snapshot.jpg",
                                content=img_resp.content,
                                mime_type="image/jpeg",
                                caption=caption_html,
                                parse_mode="HTML",
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


@receiver(post_save, sender=Alarm)
def queue_alarm_video_download(sender, instance: Alarm, created: bool, **kwargs):
    """Ставит в очередь скачивание видео-клипа, если у монитора события включена запись.

    Не скачивает синхронно (в отличие от send_alarm_to_telegram выше) — WS-сессия за
    видео занимает секунды и заблокировала бы поток SSE-парсера, создавший этот Alarm.
    """
    if not created:
        return

    monitor = instance.monitor_ref
    if not monitor or not monitor.record_video_enabled:
        return

    # Локальный импорт — избегаем циклического импорта agromash.tasks (по аналогии
    # с agromash/tasks_monitoring.py, где по той же причине импорты тоже отложены).
    from .tasks import download_alarm_video_clip_task

    download_alarm_video_clip_task.delay(instance.pk)


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


@receiver(post_save, sender=Alarm)
def upsert_plate_identities(sender, instance: Alarm, created: bool, **kwargs):
    """После сохранения Alarm(topic=PlateMatched) переносим plate_identities в нормализованную таблицу.

    Требование: `PlateIdentity.number` уникален по всей БД.
    """
    if getattr(instance, 'topic', None) != 'PlateMatched':
        return

    rows = extract_plate_rows(getattr(instance, 'plate_identities', None))
    if not rows:
        return

    for r in rows:
        number = r.get('number')
        if not number:
            continue

        defaults = {
            "state": r.get("state") or "",
            "plate_external_id": r.get("plate_external_id"),
            "owner_last_name": r.get("owner_last_name") or "",
            "owner_first_name": r.get("owner_first_name") or "",
            "owner_middle_name": r.get("owner_middle_name") or "",
            "list_external_id": r.get("list_external_id"),
            "list_name": r.get("list_name") or "",
            "list_level": r.get("list_level"),
            "last_alarm": instance,
        }

        # Защита от гонок: uniqueness по number + транзакция.
        try:
            with transaction.atomic():
                obj, created_obj = PlateIdentity.objects.get_or_create(number=number, defaults=defaults)
                if created_obj:
                    continue

                update_fields = {"last_alarm": instance}
                for k, v in defaults.items():
                    if k in ("last_alarm",):
                        continue
                    if v and not getattr(obj, k):
                        update_fields[k] = v
                if update_fields:
                    PlateIdentity.objects.filter(pk=obj.pk).update(**update_fields)
        except IntegrityError:
            # В редких случаях параллельные процессы создают одну и ту же запись.
            # Повторяем чтение и update.
            obj = PlateIdentity.objects.filter(number=number).first()
            if obj:
                PlateIdentity.objects.filter(pk=obj.pk).update(last_alarm=instance)
