from __future__ import annotations

import datetime
import io
from decimal import Decimal
from typing import Any, Optional

from django.utils import timezone
from django.conf import settings

from agromash.models import Alarm, FuelOperation
from agromash.va_api_client import VAApiClient


def _to_local_str(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return ""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    """Best-effort parse ISO datetime from API payload.

    Supports strings like:
      - 2026-01-01T10:20:30
      - 2026-01-01T10:20:30+03:00
      - 2026-01-01T10:20:30Z
    """
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if not isinstance(value, str):
        return None

    v = value.strip()
    if not v:
        return None

    # datetime.fromisoformat doesn't accept trailing "Z".
    if v.endswith("Z"):
        v = f"{v[:-1]}+00:00"

    try:
        return datetime.datetime.fromisoformat(v)
    except ValueError:
        # Fallback: try to parse just the "YYYY-mm-ddTHH:MM:SS" part.
        try:
            return datetime.datetime.fromisoformat(v[:19])
        except ValueError:
            return None


def _to_local_str_from_iso(value: Any) -> str:
    dt = _parse_iso_datetime(value)
    return _to_local_str(dt) if dt else ""


def _format_alarm_ref(alarm: dict[str, Any]) -> str:
    alarm_id = alarm.get("alarm_id") or alarm.get("id") or ""
    started = _to_local_str_from_iso(alarm.get("start_time_iso"))
    return f"{alarm_id}@{started}" if started else f"{alarm_id}@"


def _fetch_snapshot_bytes(
    *,
    snapshot_path: str,
    account_id: int,
    client_cache: dict[int, VAApiClient],
) -> Optional[bytes]:
    """Скачать snapshot из VA API и вернуть bytes (best-effort).

    Используем VAApiClient (Bearer token в БД), аналогично [`serve_snapshot()`](agromash/views.py:213).
    """
    if not snapshot_path:
        return None

    base_url = getattr(settings, "BASE_URL", None)
    if not base_url:
        return None

    client = client_cache.get(account_id)
    if client is None:
        client = VAApiClient(account_id=int(account_id), base_url=str(base_url))
        client_cache[int(account_id)] = client

    try:
        resp = client.request("GET", str(snapshot_path), timeout=(7.0, 25.0))
        try:
            if resp.status_code != 200:
                return None
            return resp.content
        finally:
            resp.close()
    except Exception:
        return None


def export_fuel_report_to_xlsx_bytes(*, report_id: int) -> bytes:
    """Сформировать XLSX по FuelOperation для указанного FuelReport.

    В выгрузку включаются как исходные поля FuelOperation, так и результаты анализа:
      - PlateIdentity (если проставлен)
      - matched_alarms (если проставлен)
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openpyxl is required for XLSX export") from e

    # Попробуем включить изображения (требуется pillow)
    try:
        from openpyxl.drawing.image import Image as XLImage
        from PIL import Image as PILImage  # type: ignore

        pillow_available = True
    except Exception:
        pillow_available = False
        XLImage = None  # type: ignore
        PILImage = None  # type: ignore

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FuelOperations"

    headers = [
        "Дата/время операции",
        "Карта (card_number)",
        "Подразделение",
        "Товар",
        "Код",
        "Количество",
        "Ед.",
        "Стоимость всего",
        "АЗС (владелец)",
        "АЗС (номер)",
        "Водитель",
        "ТС",
        "Номер авто (PlateIdentity)",
        "Гос.регион",
        "Владелец (Ф)",
        "Владелец (И)",
        "Владелец (О/card)",
        "Список",
        "Уровень",
        "Alarm (шт)",
        "Alarm (id@time)",
        "Snapshot 1",
        "Snapshot 2",
        "Snapshot 3",
        "Snapshot URL 1",
        "Snapshot URL 2",
        "Snapshot URL 3",
        "Проанализировано",
    ]

    ALARM_REF_COL_IDX = headers.index("Alarm (id@time)") + 1
    SNAPSHOT_COL_START_IDX = headers.index("Snapshot 1") + 1
    SNAPSHOT_URL_COL_START_IDX = headers.index("Snapshot URL 1") + 1

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4F81BD")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_align = Alignment(horizontal="center", vertical="center")

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{openpyxl.utils.get_column_letter(len(headers))}1"

    qs = (
        FuelOperation.objects.filter(report_id=report_id)
        .select_related("plate_identity")
        .order_by("operation_at", "id")
    )

    alarm_cache: dict[int, Optional[Alarm]] = {}
    client_cache: dict[int, VAApiClient] = {}
    max_images_total = 200
    images_added = 0

    for op in qs.iterator(chunk_size=2000):
        pi = getattr(op, "plate_identity", None)

        matched = getattr(op, "matched_alarms", None) or []
        alarms_count = len(matched)
        alarms_str = "\n".join(_format_alarm_ref(r) for r in matched)

        # Берём до 3 снимков по совпавшим Alarm (в том порядке, в каком лежат matched_alarms)
        alarm_pks: list[int] = []
        for r in matched[:3]:
            try:
                alarm_pks.append(int(r.get("id")))
            except Exception:
                continue

        snapshot_urls: list[str] = []
        snapshot_bytes_list: list[Optional[bytes]] = []
        for pk in alarm_pks:
            a = alarm_cache.get(pk)
            if a is None and pk not in alarm_cache:
                try:
                    a = (
                        Alarm.objects.only("id", "account_id", "original_quality_snapshot")
                        .select_related(None)
                        .get(pk=pk)
                    )
                except Exception:
                    a = None
                alarm_cache[pk] = a

            if not a or not getattr(a, "original_quality_snapshot", None):
                snapshot_urls.append("")
                snapshot_bytes_list.append(None)
                continue

            snap_path = str(a.original_quality_snapshot)
            snapshot_urls.append(snap_path)

            if not pillow_available or images_added >= max_images_total:
                snapshot_bytes_list.append(None)
                continue

            b = _fetch_snapshot_bytes(
                snapshot_path=snap_path,
                account_id=int(a.account_id),
                client_cache=client_cache,
            )
            snapshot_bytes_list.append(b)

        row = [
            _to_local_str(op.operation_at),
            str(op.card_number or ""),
            str(op.department_number or ""),
            str(op.product_name or ""),
            str(op.product_code or ""),
            float(op.quantity) if isinstance(op.quantity, Decimal) else (op.quantity or ""),
            str(op.unit or ""),
            float(op.total_cost) if isinstance(op.total_cost, Decimal) else (op.total_cost or ""),
            str(op.station_owner or ""),
            str(op.station_number or ""),
            str(op.driver_name or ""),
            str(op.vehicle_number or ""),
            str(getattr(pi, "number", "") or ""),
            str(getattr(pi, "state", "") or ""),
            str(getattr(pi, "owner_last_name", "") or ""),
            str(getattr(pi, "owner_first_name", "") or ""),
            str(getattr(pi, "owner_middle_name", "") or ""),
            str(getattr(pi, "list_name", "") or ""),
            getattr(pi, "list_level", "") if pi else "",
            alarms_count,
            alarms_str,
            "",
            "",
            "",
            snapshot_urls[0] if len(snapshot_urls) > 0 else "",
            snapshot_urls[1] if len(snapshot_urls) > 1 else "",
            snapshot_urls[2] if len(snapshot_urls) > 2 else "",
            _to_local_str(getattr(op, "analyzed_at", None)),
        ]
        ws.append(row)

        # Выравнивание строк отчёта по центру (кроме Alarm (id@time))
        row_idx = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            if col_idx == ALARM_REF_COL_IDX:
                continue
            ws.cell(row=row_idx, column=col_idx).alignment = data_align

        # Вставляем превьюшки снимков (best-effort)
        if pillow_available and images_added < max_images_total:
            for i, snap_bytes in enumerate(snapshot_bytes_list[:3]):
                if not snap_bytes or images_added >= max_images_total:
                    continue
                try:
                    img_stream = io.BytesIO(snap_bytes)
                    pil_img = PILImage.open(img_stream)
                    pil_img.thumbnail((240, 160))
                    out = io.BytesIO()
                    pil_img.save(out, format="PNG")
                    out.seek(0)

                    xl_img = XLImage(out)
                    col_idx = SNAPSHOT_COL_START_IDX + i
                    cell = f"{openpyxl.utils.get_column_letter(col_idx)}{row_idx}"
                    ws.add_image(xl_img, cell)
                    ws.row_dimensions[row_idx].height = 80
                    images_added += 1
                except Exception:
                    # если не получилось — остаются только URL
                    continue

    # Перенос по строкам для колонки Alarm (id@time)
    alarm_ref_alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=ALARM_REF_COL_IDX).alignment = alarm_ref_alignment

    # Перенос по строкам для URL-колонок
    url_alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    for row_idx in range(2, ws.max_row + 1):
        for col_idx in range(SNAPSHOT_URL_COL_START_IDX, SNAPSHOT_URL_COL_START_IDX + 3):
            ws.cell(row=row_idx, column=col_idx).alignment = url_alignment

    # простая подгонка ширин колонок
    widths = {
        1: 19,
        2: 14,
        3: 12,
        4: 20,
        5: 10,
        6: 11,
        7: 6,
        8: 14,
        9: 16,
        10: 10,
        11: 16,
        12: 12,
        13: 14,
        14: 10,
        15: 16,
        16: 12,
        17: 14,
        18: 14,
        19: 8,
        20: 9,
        21: 40,
        22: 18,
        23: 18,
        24: 18,
        25: 40,
        26: 40,
        27: 40,
        28: 19,
    }
    for idx, w in widths.items():
        ws.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = w

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
