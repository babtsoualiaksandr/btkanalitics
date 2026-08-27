"""Django model field, прозрачно шифрующий значение в БД (Fernet).

Приложение продолжает работать с обычной строкой в Python-коде
(например, va_api_client.py читает account.password как plaintext) —
шифрование/расшифровка происходят на границе БД.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _cipher() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode() if isinstance(settings.FIELD_ENCRYPTION_KEY, str) else settings.FIELD_ENCRYPTION_KEY)


class EncryptedCharField(models.CharField):
    """CharField, хранящий значение в БД зашифрованным (Fernet)."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return _cipher().encrypt(value.encode("utf-8")).decode("ascii")

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError):
            # Значение не похоже на наш Fernet-токен (например, ещё не
            # мигрированный plaintext, который может содержать символы вне
            # url-safe base64 алфавита — тогда сам base64-decode внутри
            # Fernet.decrypt падает с ValueError/binascii.Error, а не
            # InvalidToken). Возвращаем как есть, чтобы не уронить чтение
            # существующих данных на переходном этапе до data-миграции.
            return value
