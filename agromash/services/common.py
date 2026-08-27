"""Мелкие утилиты, общие для views/admin/views_events.

Вынесены сюда, т.к. раньше были продублированы в трёх местах.
"""

import datetime
from typing import Optional

from django.core.exceptions import PermissionDenied


def assert_events_access(user) -> None:
    """Проверка доступа к странице событий (используется в views.py и views_events.py)."""
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return
    if user.has_perm('agromash.can_view_events'):
        return
    raise PermissionDenied

# Alarm.start_time/end_time в данных VA приходят то в секундах, то в
# миллисекундах. Эвристика различения: значения >= этого порога считаем
# миллисекундами. (~ год 2001 в мс, далеко за пределами разумных секундных
# timestamp — безопасный порог для реальных данных).
ALARM_EPOCH_MS_THRESHOLD = 1_000_000_000_000


def alarm_epoch_to_aware_dt(value: Optional[int]) -> Optional[datetime.datetime]:
    """BigInteger epoch (сек или мс) -> aware datetime (UTC).

    Используется для Alarm.start_time/end_time.
    """
    if value is None:
        return None
    ts = int(value)
    if ts > ALARM_EPOCH_MS_THRESHOLD:
        ts = ts / 1000.0
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
