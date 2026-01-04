"""Мини-клиент для отправки сообщений/файлов в Telegram Bot API."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


def _safe_log_event(
    *,
    chat_id: int,
    kind: str,
    ok: bool,
    status_code: Optional[int] = None,
    error: str = "",
    text: str = "",
    filename: str = "",
    alarm=None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort запись события отправки в БД.

    Важно: не должно ронять отправку Telegram, поэтому все исключения глотаем.
    """

    try:
        from agromash.models import TelegramEventLog, TelegramSubscriber

        sub = TelegramSubscriber.objects.filter(chat_id=chat_id).first()
        TelegramEventLog.objects.create(
            subscriber=sub,
            chat_id=chat_id,
            kind=kind,
            ok=ok,
            status_code=status_code,
            error=(error or "")[:4000],
            text=(text or "")[:4000],
            filename=(filename or "")[:255],
            alarm=alarm,
            meta=meta or {},
        )
    except Exception:
        logger.exception("Failed to write TelegramEventLog (chat_id=%s)", chat_id)


def _get_token() -> Optional[str]:
    return getattr(settings, "TLG_BOT_TOKEN", None)


def send_message(*, chat_id: int, text: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = _get_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "TLG_BOT_TOKEN is not set"}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        ok = bool(getattr(resp, "ok", False))
        _safe_log_event(
            chat_id=chat_id,
            kind="message",
            ok=ok,
            status_code=getattr(resp, "status_code", None),
            error="" if ok else (resp.text or ""),
            text=text,
            meta=meta,
        )
        return {"ok": ok, "status_code": getattr(resp, "status_code", None), "error": "" if ok else (resp.text or "")}
    except Exception:
        logger.exception("Ошибка отправки Telegram message (chat_id=%s)", chat_id)
        _safe_log_event(chat_id=chat_id, kind="message", ok=False, error="exception", text=text, meta=meta)
        return {"ok": False, "status_code": None, "error": "exception"}


def send_document(
    *,
    chat_id: int,
    filename: str,
    content: bytes,
    caption: Optional[str] = None,
    mime_type: str = "application/octet-stream",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Отправить файл как документ (подходит для PDF/XLSX)."""

    token = _get_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "TLG_BOT_TOKEN is not set"}

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    files = {
        "document": (filename, content, mime_type),
    }

    try:
        resp = requests.post(url, data=data, files=files, timeout=30)
        ok = bool(getattr(resp, "ok", False))
        _safe_log_event(
            chat_id=chat_id,
            kind="document",
            ok=ok,
            status_code=getattr(resp, "status_code", None),
            error="" if ok else (resp.text or ""),
            text=caption or "",
            filename=filename,
            meta=meta,
        )
        return {"ok": ok, "status_code": getattr(resp, "status_code", None), "error": "" if ok else (resp.text or "")}
    except Exception:
        logger.exception("Ошибка отправки Telegram document (chat_id=%s, filename=%s)", chat_id, filename)
        _safe_log_event(chat_id=chat_id, kind="document", ok=False, error="exception", text=caption or "", filename=filename, meta=meta)
        return {"ok": False, "status_code": None, "error": "exception"}


def send_photo(
    *,
    chat_id: int,
    filename: str,
    content: bytes,
    caption: str,
    mime_type: str = "image/jpeg",
    alarm=None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Отправить фото (для уведомлений по тревогам)."""

    token = _get_token()
    if not token:
        return {"ok": False, "status_code": None, "error": "TLG_BOT_TOKEN is not set"}

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption}
    files = {"photo": (filename, content, mime_type)}

    try:
        resp = requests.post(url, data=data, files=files, timeout=30)
        ok = bool(getattr(resp, "ok", False))
        _safe_log_event(
            chat_id=chat_id,
            kind="photo",
            ok=ok,
            status_code=getattr(resp, "status_code", None),
            error="" if ok else (resp.text or ""),
            text=caption,
            filename=filename,
            alarm=alarm,
            meta=meta,
        )
        return {"ok": ok, "status_code": getattr(resp, "status_code", None), "error": "" if ok else (resp.text or "")}
    except Exception:
        logger.exception("Ошибка отправки Telegram photo (chat_id=%s, filename=%s)", chat_id, filename)
        _safe_log_event(chat_id=chat_id, kind="photo", ok=False, error="exception", text=caption, filename=filename, alarm=alarm, meta=meta)
        return {"ok": False, "status_code": None, "error": "exception"}
