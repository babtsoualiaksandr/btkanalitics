from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple


logger = logging.getLogger(__name__)


def normalize_plate_number(value: str) -> str:
    # унифицируем для уникальности
    return (value or "").strip().upper().replace(" ", "")


def iter_plate_identities(plate_identities: Any) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Итератор по структуре Alarm.plate_identities.

    Ожидаемый формат:
      [
        {
          "list": {"id": 104, "name": "БУЭС", "level": 1},
          "plates": [{"id": 1021, "state": "BY", "number": "5189EC6", ...}],
        },
      ]

    Возвращает пары (list_dict, plate_dict).
    """
    if not plate_identities:
        return
    if not isinstance(plate_identities, list):
        return

    for item in plate_identities:
        if not isinstance(item, dict):
            continue
        list_info = item.get("list") or {}
        plates = item.get("plates") or []
        if not isinstance(list_info, dict) or not isinstance(plates, list):
            continue
        for p in plates:
            if isinstance(p, dict):
                yield list_info, p


def extract_plate_rows(plate_identities: Any) -> List[Dict[str, Any]]:
    """Преобразовать plate_identities -> список нормализованных строк для upsert."""
    rows: List[Dict[str, Any]] = []
    for list_info, p in iter_plate_identities(plate_identities):
        num = normalize_plate_number(str(p.get("number") or ""))
        if not num:
            continue
        rows.append(
            {
                "number": num,
                "state": str(p.get("state") or "").strip().upper(),
                "plate_external_id": p.get("id"),
                "owner_last_name": str(p.get("owner_last_name") or "").strip(),
                "owner_first_name": str(p.get("owner_first_name") or "").strip(),
                "owner_middle_name": str(p.get("owner_middle_name") or "").strip(),
                "list_external_id": list_info.get("id"),
                "list_name": str(list_info.get("name") or "").strip(),
                "list_level": list_info.get("level"),
            }
        )
    return rows

