from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Tuple

from django.db.models import Max, Min, Q
from django.utils import timezone

from agromash.models import Alarm, FuelOperation, FuelReport, PlateIdentity
from agromash.services.plate_identities import iter_plate_identities, normalize_plate_number


logger = logging.getLogger(__name__)


def _ensure_aware_utc(dt: datetime.datetime) -> datetime.datetime:
    """Привести datetime к aware UTC."""
    if dt is None:
        raise ValueError("dt is None")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _alarm_ts_to_aware_utc(value: int) -> Optional[datetime.datetime]:
    """Alarm.start_time (сек/мс epoch) -> aware UTC datetime."""
    if value is None:
        return None
    ts = int(value)
    # Эвристика как в admin: > 1e12 считаем миллисекундами.
    if ts > 1_000_000_000_000:
        ts = ts / 1000.0
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


def _iter_alarm_plate_numbers(alarm: Alarm) -> Iterable[str]:
    """Из Alarm.plate_identities извлечь нормализованные номера."""
    for _list_info, plate in iter_plate_identities(getattr(alarm, "plate_identities", None)):
        num = normalize_plate_number(str((plate or {}).get("number") or ""))
        if num:
            yield num


@dataclass(frozen=True)
class FuelReportAnalyzeSummary:
    report_id: int
    operations_total: int
    operations_updated: int
    operations_with_plate_identity: int
    operations_with_alarms: int
    alarms_candidates: int


def analyze_fuel_report(*, report_id: int, window_minutes: int = 10) -> FuelReportAnalyzeSummary:
    """Анализ операций FuelOperation внутри FuelReport.

    Логика:
      1) для каждой FuelOperation подбираем PlateIdentity по связке:
         FuelOperation.card_number == PlateIdentity.owner_middle_name
      2) по PlateIdentity.number подбираем Alarm, у которых в plate_identities есть
         этот номер, и Alarm.start_time попадает в окно +/- window_minutes относительно
         FuelOperation.operation_at

    Результат пишется обратно в FuelOperation поля:
      - plate_identity (FK)
      - matched_alarms (JSON list)
      - analyzed_at (datetime)
    """

    report_exists = FuelReport.objects.filter(pk=report_id).exists()
    if not report_exists:
        logger.warning("analyze_fuel_report: report not found report_id=%s", report_id)
        return FuelReportAnalyzeSummary(
            report_id=report_id,
            operations_total=0,
            operations_updated=0,
            operations_with_plate_identity=0,
            operations_with_alarms=0,
            alarms_candidates=0,
        )

    ops_qs = FuelOperation.objects.filter(report_id=report_id)
    ops_total = ops_qs.count()
    if ops_total == 0:
        return FuelReportAnalyzeSummary(
            report_id=report_id,
            operations_total=0,
            operations_updated=0,
            operations_with_plate_identity=0,
            operations_with_alarms=0,
            alarms_candidates=0,
        )

    window = datetime.timedelta(minutes=int(window_minutes))
    now = timezone.now()

    # 1) диапазон времени по операциям (чтобы ограничить выборку Alarm)
    bounds = ops_qs.aggregate(min_dt=Min("operation_at"), max_dt=Max("operation_at"))
    min_dt = bounds.get("min_dt")
    max_dt = bounds.get("max_dt")
    if not min_dt or not max_dt:
        # странно, но на всякий случай
        min_dt = now
        max_dt = now

    start_dt = _ensure_aware_utc(min_dt) - window
    end_dt = _ensure_aware_utc(max_dt) + window

    start_sec = int(start_dt.timestamp())
    end_sec = int(end_dt.timestamp())
    start_ms = start_sec * 1000
    end_ms = end_sec * 1000

    # 2) карточки из FuelOperation -> PlateIdentity
    cards: List[str] = []
    for v in ops_qs.values_list("card_number", flat=True).distinct().iterator():
        s = str(v or "").strip()
        if s:
            cards.append(s)
    cards = list(dict.fromkeys(cards))

    pi_by_card: Dict[str, PlateIdentity] = {}
    if cards:
        for pi in (
            PlateIdentity.objects.filter(owner_middle_name__in=cards)
            .only("id", "number", "owner_middle_name")
            .iterator(chunk_size=2000)
        ):
            key = str(pi.owner_middle_name or "").strip()
            if key and key not in pi_by_card:
                pi_by_card[key] = pi

    # 3) кандидатные Alarm за общий диапазон, собираем индекс plate_number -> alarms
    alarms_q = (
        Alarm.objects.filter(topic="PlateMatched")
        .exclude(plate_identities__isnull=True)
        .filter(
            Q(start_time__gte=start_sec, start_time__lte=end_sec)
            | Q(start_time__gte=start_ms, start_time__lte=end_ms)
        )
        .only("id", "alarm_id", "start_time", "plate_identities", "original_quality_snapshot")
    )

    alarms_by_plate: DefaultDict[str, List[Tuple[Alarm, datetime.datetime]]] = defaultdict(list)
    alarms_candidates = 0
    for alarm in alarms_q.iterator(chunk_size=2000):
        alarms_candidates += 1
        alarm_dt = _alarm_ts_to_aware_utc(alarm.start_time)
        if not alarm_dt:
            continue
        for plate_number in _iter_alarm_plate_numbers(alarm):
            alarms_by_plate[plate_number].append((alarm, alarm_dt))

    # 4) обновляем операции батчами
    updated = 0
    with_pi = 0
    with_alarms = 0
    batch: List[FuelOperation] = []

    ops_iter = (
        ops_qs.only("id", "card_number", "operation_at")
        .select_related(None)
        .iterator(chunk_size=2000)
    )
    for op in ops_iter:
        card = str(op.card_number or "").strip()
        pi = pi_by_card.get(card)

        op.plate_identity = pi
        if pi:
            with_pi += 1

        op_dt = _ensure_aware_utc(op.operation_at)

        matched_rows: List[Dict[str, Any]] = []
        snapshot_urls: List[str] = []
        if pi and pi.number:
            for alarm, alarm_dt in alarms_by_plate.get(str(pi.number).strip().upper(), []):
                delta = abs((alarm_dt - op_dt).total_seconds())
                if delta <= window.total_seconds():
                    matched_rows.append(
                        {
                            "id": alarm.id,
                            "alarm_id": alarm.alarm_id,
                            "start_time": alarm.start_time,
                            "start_time_iso": alarm_dt.isoformat(),
                            "delta_seconds": int(delta),
                        }
                    )
                    snap = getattr(alarm, "original_quality_snapshot", None)
                    if snap:
                        snapshot_urls.append(str(snap))

        if matched_rows:
            with_alarms += 1

        # стабильный порядок (по времени/дельте)
        matched_rows.sort(key=lambda r: (r.get("delta_seconds", 0), r.get("start_time", 0)))
        op.matched_alarms = matched_rows
        # best-effort: список URL/путей на original_quality_snapshot для совпавших тревог
        op.matched_alarm_snapshot_urls = snapshot_urls
        op.analyzed_at = now

        batch.append(op)
        if len(batch) >= 1000:
            FuelOperation.objects.bulk_update(
                batch,
                ["plate_identity", "matched_alarms", "matched_alarm_snapshot_urls", "analyzed_at"],
                batch_size=1000,
            )
            updated += len(batch)
            batch.clear()

    if batch:
        FuelOperation.objects.bulk_update(
            batch,
            ["plate_identity", "matched_alarms", "matched_alarm_snapshot_urls", "analyzed_at"],
            batch_size=1000,
        )
        updated += len(batch)

    return FuelReportAnalyzeSummary(
        report_id=report_id,
        operations_total=int(ops_total),
        operations_updated=int(updated),
        operations_with_plate_identity=int(with_pi),
        operations_with_alarms=int(with_alarms),
        alarms_candidates=int(alarms_candidates),
    )
