"""Расчёт времени следующей отправки отчёта."""

from __future__ import annotations

import calendar
import datetime
from typing import Optional

from django.utils import timezone


def compute_next_run_at(*, now: Optional[datetime.datetime], frequency: str) -> datetime.datetime:
    now = now or timezone.now()

    if frequency == "10m":
        return now + datetime.timedelta(minutes=10)
    if frequency == "hourly":
        return now + datetime.timedelta(hours=1)
    if frequency == "daily":
        return now + datetime.timedelta(days=1)
    if frequency == "weekly":
        return now + datetime.timedelta(days=7)
    if frequency == "monthly":
        return _add_one_month(now)

    # fallback
    return now + datetime.timedelta(hours=1)


def _add_one_month(dt: datetime.datetime) -> datetime.datetime:
    """Добавить 1 календарный месяц, сохранив время суток."""
    year = dt.year
    month = dt.month + 1
    if month > 12:
        month = 1
        year += 1

    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)

