"""Мини-клиент для отправки сообщений/файлов в Telegram Bot API."""

from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


def _get_token() -> Optional[str]:
    return getattr(settings, "TLG_BOT_TOKEN", None)


def send_message(*, chat_id: int, text: str) -> None:
    token = _get_token()
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception:
        logger.exception("Ошибка отправки Telegram message (chat_id=%s)", chat_id)


def send_document(
    *,
    chat_id: int,
    filename: str,
    content: bytes,
    caption: Optional[str] = None,
    mime_type: str = "application/octet-stream",
) -> None:
    """Отправить файл как документ (подходит для PDF/XLSX)."""

    token = _get_token()
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    files = {
        "document": (filename, content, mime_type),
    }

    try:
        requests.post(url, data=data, files=files, timeout=30)
    except Exception:
        logger.exception("Ошибка отправки Telegram document (chat_id=%s, filename=%s)", chat_id, filename)

