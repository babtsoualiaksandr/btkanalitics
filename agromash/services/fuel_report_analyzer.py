from __future__ import annotations

import datetime
import logging
import bisect
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Optional, Tuple

from django.db.models import Max, Min, Q
from django.utils import timezone

from agromash.models import Alarm, FuelOperation, FuelReport, PlateIdentity
from agromash.services.plate_identities import iter_plate_identities, normalize_plate_number


logger = logging.getLogger(__name__)


def _normalize_station_token(token: str) -> str:
    """Костыль: если токен содержит 'АЗС', считаем его равным '64'."""
    t = str(token or "").strip()
    if "АЗС" in t or "азс" in t.lower():
        return "64"
    return t


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


def _iter_alarm_plate_objects(alarm: Alarm) -> Iterable[Dict[str, Any]]:
    """Из Alarm.plate_identities извлечь plate-объекты (для сохранения в FuelOperation.fallback_plate_numbers).

    Формат plate (пример):
      {"id": 1028, "state": "BY", "number": "AM18676", "owner_last_name": "...", ...}
    """
    for _list_info, plate in iter_plate_identities(getattr(alarm, "plate_identities", None)):
        if not isinstance(plate, dict):
            continue
        yield {
            "id": plate.get("id"),
            "state": plate.get("state"),
            "number": plate.get("number"),
            "owner_last_name": plate.get("owner_last_name"),
            "owner_first_name": plate.get("owner_first_name"),
            "owner_middle_name": plate.get("owner_middle_name"),
        }


@dataclass(frozen=True)
class FuelReportAnalyzeSummary:
    report_id: int
    operations_total: int
    operations_updated: int
    operations_with_plate_identity: int
    operations_with_alarms: int
    alarms_candidates: int


def analyze_fuel_report(
    *,
    report_id: int,
    window_minutes: int = 10,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> FuelReportAnalyzeSummary:
    """Анализ операций FuelOperation внутри FuelReport.

    Логика:
      1) для каждой FuelOperation подбираем PlateIdentity по связке:
         FuelOperation.card_number == PlateIdentity.owner_middle_name
      2) для каждой FuelOperation подбираем Alarm по условиям:
         - Alarm.monitor_name_second_token == FuelOperation.station_number
         - Alarm.start_time попадает в окно +/- window_minutes относительно FuelOperation.operation_at
         - приоритет: если FuelOperation.card_number входит в список owner_middle_name внутри Alarm.plate_identities,
           то выбираем только такие Alarm; иначе используем match только по station/time.

     Для ускорения выборки Alarm используем период FuelReport.period_start/period_end
     (если задан), иначе вычисляем границы по времени операций FuelOperation.

    Результат пишется обратно в FuelOperation поля:
      - plate_identity (FK)
      - matched_alarms (JSON list, также содержит snapshot_url best-effort)
      - analyzed_at (datetime)
    """

    logger.info("analyze_fuel_report START report_id=%s window_minutes=%s", report_id, window_minutes)

    report = FuelReport.objects.filter(pk=report_id).only("id", "period_start", "period_end").first()
    if not report:
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
    if progress_cb:
        progress_cb(0, ops_total)
    if ops_total == 0:
        logger.info("analyze_fuel_report: no operations report_id=%s", report_id)
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

    # 1) диапазон времени для выборки Alarm
    #    Пытаемся использовать FuelReport.period_start/period_end (date), иначе — по времени операций.
    if getattr(report, "period_start", None) and getattr(report, "period_end", None):
        tz = timezone.get_current_timezone()
        start_local = timezone.make_aware(
            datetime.datetime.combine(report.period_start, datetime.time.min),
            timezone=tz,
        )
        end_local = timezone.make_aware(
            datetime.datetime.combine(report.period_end, datetime.time.max),
            timezone=tz,
        )
        start_dt = _ensure_aware_utc(start_local) - window
        end_dt = _ensure_aware_utc(end_local) + window
    else:
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

    logger.info(
        "analyze_fuel_report step=time_range report_id=%s start=%s end=%s",
        report_id, start_dt.isoformat(), end_dt.isoformat(),
    )

    # 2) карточки из FuelOperation -> PlateIdentity
    cards: List[str] = []
    for v in ops_qs.values_list("card_number", flat=True).distinct().iterator():
        s = str(v or "").strip()
        if s:
            cards.append(s)
    cards = list(dict.fromkeys(cards))

    logger.info("analyze_fuel_report step=cards_loaded report_id=%s cards=%s", report_id, len(cards))

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

    logger.info(
        "analyze_fuel_report step=plate_identities_loaded report_id=%s matched=%s",
        report_id, len(pi_by_card),
    )

    # 3) список station_number из операций (для потенциальной оптимизации/статистики)
    station_numbers: List[str] = []
    for v in ops_qs.values_list("station_number", flat=True).distinct().iterator():
        s = str(v or "").strip()
        if s:
            station_numbers.append(s)
    station_numbers = list(dict.fromkeys(station_numbers))
    station_numbers_set = set(station_numbers)

    logger.info("analyze_fuel_report step=stations_loaded report_id=%s stations=%s", report_id, len(station_numbers))

    # 4) кандидатные Alarm за общий диапазон, собираем индекс monitor_name_second_token -> alarms
    alarms_q = (
        Alarm.objects.filter(topic="PlateMatched")
        .filter(
            Q(start_time__gte=start_sec, start_time__lte=end_sec)
            | Q(start_time__gte=start_ms, start_time__lte=end_ms)
        )
        .only(
            "id",
            "alarm_id",
            "start_time",
            "monitor_name",
            "plate_identities",
            "original_quality_snapshot",
        )
    )

    alarms_by_station: DefaultDict[str, List[Tuple[Alarm, datetime.datetime, set[str]]]] = defaultdict(list)
    alarms_candidates = 0
    for alarm in alarms_q.iterator(chunk_size=2000):
        alarms_candidates += 1
        alarm_dt = _alarm_ts_to_aware_utc(alarm.start_time)
        if not alarm_dt:
            continue

        token = str(getattr(alarm, "monitor_name_second_token", "") or "").strip()
        token = _normalize_station_token(token)
        if token and (not station_numbers_set or token in station_numbers_set):
            owners: set[str] = set()
            for _list_info, p in iter_plate_identities(getattr(alarm, "plate_identities", None)):
                om = str((p or {}).get("owner_middle_name") or "").strip()
                if om:
                    owners.add(om)
            alarms_by_station[token].append((alarm, alarm_dt, owners))

    logger.info(
        "analyze_fuel_report step=alarms_loaded report_id=%s alarms_candidates=%s stations_with_alarms=%s",
        report_id, alarms_candidates, len(alarms_by_station),
    )

    # Индексы по времени внутри каждой станции для быстрого поиска окна.
    alarms_by_station_dts: Dict[str, List[datetime.datetime]] = {}
    for station, pairs in alarms_by_station.items():
        pairs.sort(key=lambda p: p[1])
        alarms_by_station_dts[station] = [dt for _a, dt, _owners in pairs]

    # 5) обновляем операции батчами
    updated = 0
    with_pi = 0
    with_alarms = 0
    batch: List[FuelOperation] = []

    ops_iter = (
        ops_qs.only(
            "id",
            "card_number",
            "operation_at",
            "station_number",
            "fallback_plate_numbers",
        )
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
        # key -> plate dict
        fallback_plate_numbers_map: Dict[str, Dict[str, Any]] = {}

        station = str(getattr(op, "station_number", "") or "").strip()
        station = _normalize_station_token(station)
        pairs = alarms_by_station.get(station) if station else None
        if pairs:
            start_w = op_dt - window
            end_w = op_dt + window
            dts = alarms_by_station_dts.get(station) or []
            left = bisect.bisect_left(dts, start_w)
            right = bisect.bisect_right(dts, end_w)

            strict_rows: List[Dict[str, Any]] = []
            loose_rows: List[Dict[str, Any]] = []

            for alarm, alarm_dt, owners in pairs[left:right]:
                delta = abs((alarm_dt - op_dt).total_seconds())
                snap = getattr(alarm, "original_quality_snapshot", None)
                if not pi:
                    for plate_obj in _iter_alarm_plate_objects(alarm):
                        num_key = normalize_plate_number(str(plate_obj.get("number") or ""))
                        owner_key = str(plate_obj.get("owner_middle_name") or "").strip()
                        key = f"{owner_key}|{num_key}" if owner_key or num_key else ""
                        if key and key not in fallback_plate_numbers_map:
                            fallback_plate_numbers_map[key] = plate_obj

                row = {
                    "id": alarm.id,
                    "alarm_id": alarm.alarm_id,
                    "start_time": alarm.start_time,
                    "start_time_iso": alarm_dt.isoformat(),
                    "delta_seconds": int(delta),
                    "snapshot_url": str(snap) if snap else "",
                }

                if card and owners and card in owners:
                    strict_rows.append(row)
                else:
                    loose_rows.append(row)

            matched_rows = strict_rows if strict_rows else loose_rows

        if matched_rows:
            with_alarms += 1

        # стабильный порядок (по времени/дельте)
        matched_rows.sort(key=lambda r: (r.get("delta_seconds", 0), r.get("start_time", 0)))
        op.matched_alarms = matched_rows
        # best-effort: список URL/путей на original_quality_snapshot для совпавших тревог
        snapshot_urls = [str(r.get("snapshot_url") or "") for r in matched_rows]
        op.matched_alarm_snapshot_urls = snapshot_urls

        # Fallback номера: заполняем только если PlateIdentity не подобран.
        if not pi and fallback_plate_numbers_map:
            op.fallback_plate_numbers = [fallback_plate_numbers_map[k] for k in sorted(fallback_plate_numbers_map.keys())]
        else:
            op.fallback_plate_numbers = []
        op.analyzed_at = now

        batch.append(op)
        if len(batch) >= 1000:
            FuelOperation.objects.bulk_update(
                batch,
                [
                    "plate_identity",
                    "matched_alarms",
                    "matched_alarm_snapshot_urls",
                    "fallback_plate_numbers",
                    "analyzed_at",
                ],
                batch_size=1000,
            )
            updated += len(batch)
            logger.info(
                "analyze_fuel_report progress report_id=%s updated=%s/%s",
                report_id, updated, ops_total,
            )
            if progress_cb:
                progress_cb(updated, ops_total)
            batch.clear()

    if batch:
        FuelOperation.objects.bulk_update(
            batch,
            [
                "plate_identity",
                "matched_alarms",
                "matched_alarm_snapshot_urls",
                "fallback_plate_numbers",
                "analyzed_at",
            ],
            batch_size=1000,
        )
        updated += len(batch)

    if progress_cb:
        progress_cb(updated, ops_total)

    logger.info(
        "analyze_fuel_report DONE report_id=%s total=%s updated=%s with_pi=%s with_alarms=%s alarms_candidates=%s",
        report_id, ops_total, updated, with_pi, with_alarms, alarms_candidates,
    )

    return FuelReportAnalyzeSummary(
        report_id=report_id,
        operations_total=int(ops_total),
        operations_updated=int(updated),
        operations_with_plate_identity=int(with_pi),
        operations_with_alarms=int(with_alarms),
        alarms_candidates=int(alarms_candidates),
    )
