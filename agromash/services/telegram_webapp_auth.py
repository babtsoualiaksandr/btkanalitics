"""Telegram WebApp (Mini App) auth helpers.

Проверяем подпись `initData` согласно документации Telegram Web Apps:
  https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Мы используем это для авторизации пользователя без Django-сессий.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl


class TelegramInitDataError(Exception):
    pass


@dataclass(frozen=True)
class TelegramWebAppUser:
    id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""


@dataclass(frozen=True)
class TelegramWebAppInitData:
    raw: str
    auth_date: int
    user: Optional[TelegramWebAppUser]
    query_id: str = ""


def _parse_user(raw_user: str) -> Optional[TelegramWebAppUser]:
    if not raw_user:
        return None
    try:
        obj = json.loads(raw_user)
    except Exception as e:
        raise TelegramInitDataError(f"invalid user json: {e}")

    try:
        uid = int(obj.get("id"))
    except Exception:
        raise TelegramInitDataError("user.id missing")

    return TelegramWebAppUser(
        id=uid,
        username=str(obj.get("username") or ""),
        first_name=str(obj.get("first_name") or ""),
        last_name=str(obj.get("last_name") or ""),
    )


def validate_webapp_init_data(
    *,
    init_data: str,
    bot_token: str,
    max_age_sec: int = 24 * 60 * 60,
    now_ts: Optional[int] = None,
) -> TelegramWebAppInitData:
    """Валидирует initData и возвращает распарсенные данные.

    Raises:
        TelegramInitDataError: если подпись/формат невалидны.
    """

    init_data = (init_data or "").strip()
    if not init_data:
        raise TelegramInitDataError("empty initData")

    bot_token = (bot_token or "").strip()
    if not bot_token:
        raise TelegramInitDataError("bot token is not configured")

    pairs = list(parse_qsl(init_data, keep_blank_values=True))
    data: Dict[str, str] = {k: v for k, v in pairs}

    received_hash = (data.get("hash") or "").strip()
    if not received_hash:
        raise TelegramInitDataError("hash missing")

    # Строка проверки: отсортированные key=value, кроме hash
    check_items = [(k, v) for k, v in data.items() if k != "hash"]
    check_items.sort(key=lambda x: x[0])
    data_check_string = "\n".join([f"{k}={v}" for k, v in check_items])

    # ВАЖНО: для Telegram WebApp используется ключ, отличный от Login Widget.
    # secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise TelegramInitDataError("invalid signature")

    try:
        auth_date = int(data.get("auth_date") or "0")
    except Exception:
        raise TelegramInitDataError("invalid auth_date")
    if not auth_date:
        raise TelegramInitDataError("auth_date missing")

    now_ts = int(now_ts if now_ts is not None else time.time())
    if max_age_sec and auth_date < (now_ts - int(max_age_sec)):
        raise TelegramInitDataError("initData expired")

    return TelegramWebAppInitData(
        raw=init_data,
        auth_date=auth_date,
        user=_parse_user(str(data.get("user") or "")),
        query_id=str(data.get("query_id") or ""),
    )
