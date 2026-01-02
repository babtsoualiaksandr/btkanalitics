"""Парсер payload'а алерта VideoAnalytics.

Задача: привести поле Alarm.data (сырое JSON-событие) к удобному, стабильному виду,
чтобы дальше использовать в уведомлениях/логике.

Поддерживаемые topic (минимум):
  - LineCrossed
  - PlateNotMatched
  - FaceNotMatched

Если topic неизвестен — возвращаем базовые поля + raw params.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional


def _get_in(obj: Any, path: Iterable[Any], default: Any = None) -> Any:
    """Безопасно пройти по вложенным dict/list структурам."""
    cur = obj
    for key in path:
        try:
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list) and isinstance(key, int):
                cur = cur[key]
            else:
                return default
        except (KeyError, IndexError, TypeError):
            return default
        if cur is None:
            return default
    return cur


def _as_str_list(tags: Any) -> List[str]:
    if not isinstance(tags, list):
        return []
    out: List[str] = []
    for t in tags:
        if isinstance(t, dict) and t.get("name"):
            out.append(str(t["name"]))
        elif isinstance(t, str):
            out.append(t)
    return out


def _pick_original_snapshot(alarm: Dict[str, Any]) -> Optional[str]:
    # Иногда original_quality_snapshot лежит на верхнем уровне, иногда — в snapshots[0].
    direct = alarm.get("original_quality_snapshot")
    if isinstance(direct, str) and direct:
        return direct
    from_snapshots = _get_in(alarm, ["snapshots", 0, "original_quality_snapshot"], None)
    if isinstance(from_snapshots, str) and from_snapshots:
        return from_snapshots
    return None


@dataclass(frozen=True)
class ParsedAlarm:
    """Нормализованное представление алерта."""

    alarm_id: Optional[str]
    topic: str
    level: Optional[int]
    module: Optional[str]
    event_id: Optional[int]
    monitor_id: Optional[int]
    monitor_name: str
    channel_id: Optional[int]
    channel_name: str
    source_name: str
    start_time: Optional[int]
    end_time: Optional[int]
    tags: List[str]
    original_quality_snapshot: Optional[str]
    snapshots: List[Dict[str, Any]]
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_alarm_data(alarm: Dict[str, Any]) -> ParsedAlarm:
    """Распарсить сырое поле Alarm.data в ParsedAlarm.

    Вход: dict, как приходит из /api/v2/alarm-monitors/.../alarms/search
    """
    topic = str(alarm.get("topic") or "")
    params = alarm.get("params") if isinstance(alarm.get("params"), dict) else {}

    details: Dict[str, Any]
    if topic == "LineCrossed":
        details = _parse_line_crossed(params)
    elif topic == "PlateNotMatched":
        details = _parse_plate_not_matched(params)
    elif topic == "FaceNotMatched":
        details = _parse_face_not_matched(params)
    else:
        details = {
            "raw_params": params,
        }

    snapshots = alarm.get("snapshots") if isinstance(alarm.get("snapshots"), list) else []
    norm_snapshots: List[Dict[str, Any]] = []
    for s in snapshots:
        if not isinstance(s, dict):
            continue
        norm_snapshots.append(
            {
                "tag": s.get("tag"),
                "type": s.get("type"),
                "path": s.get("path"),
                "original_quality_snapshot": s.get("original_quality_snapshot"),
                "metadata": s.get("metadata") if isinstance(s.get("metadata"), dict) else {},
            }
        )

    return ParsedAlarm(
        alarm_id=alarm.get("id"),
        topic=topic,
        level=alarm.get("level") if isinstance(alarm.get("level"), int) else None,
        module=alarm.get("module") if isinstance(alarm.get("module"), str) else None,
        event_id=alarm.get("event_id") if isinstance(alarm.get("event_id"), int) else None,
        monitor_id=alarm.get("monitor_id") if isinstance(alarm.get("monitor_id"), int) else None,
        monitor_name=str(alarm.get("monitor_name") or ""),
        channel_id=alarm.get("channel_id") if isinstance(alarm.get("channel_id"), int) else None,
        channel_name=str(alarm.get("channel_name") or ""),
        source_name=str(alarm.get("source_name") or ""),
        start_time=alarm.get("start_time") if isinstance(alarm.get("start_time"), int) else None,
        end_time=alarm.get("end_time") if isinstance(alarm.get("end_time"), int) else None,
        tags=_as_str_list(alarm.get("tags")),
        original_quality_snapshot=_pick_original_snapshot(alarm),
        snapshots=norm_snapshots,
        details=details,
    )


def _parse_line_crossed(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = params.get("object") if isinstance(params.get("object"), dict) else {}
    classes = obj.get("classes") if isinstance(obj.get("classes"), list) else []
    best_class = None
    best_similarity = None
    for c in classes:
        if not isinstance(c, dict):
            continue
        sim = c.get("similarity")
        if not isinstance(sim, (int, float)):
            continue
        if best_similarity is None or sim > best_similarity:
            best_similarity = sim
            best_class = c.get("class")

    # tripwire обычно лежит в metadata snapshots[0]
    tripwire = _get_in(params, ["snapshots", 0, "metadata", "tripwire"], None)
    return {
        "object_id": obj.get("id"),
        "best_class": best_class,
        "classes": classes,
        "position": obj.get("position") if isinstance(obj.get("position"), dict) else {},
        "bounding_box": obj.get("bounding_box") if isinstance(obj.get("bounding_box"), dict) else {},
        "tripwire": tripwire if isinstance(tripwire, list) else [],
        "reliability": params.get("reliability"),
    }


def _parse_plate_not_matched(params: Dict[str, Any]) -> Dict[str, Any]:
    plate = params.get("plate") if isinstance(params.get("plate"), dict) else {}
    obj = params.get("object") if isinstance(params.get("object"), dict) else {}

    object_type = obj.get("object_type") if isinstance(obj.get("object_type"), dict) else {}
    return {
        "plate_state": plate.get("state"),
        "plate_valid": plate.get("valid"),
        "plate_number": plate.get("number"),
        "recognition_time": plate.get("recognition_time"),
        "object_id": obj.get("id"),
        "object_type": object_type.get("value"),
        "bounding_box": obj.get("bounding_box") if isinstance(obj.get("bounding_box"), dict) else {},
        "identities": params.get("identities") if isinstance(params.get("identities"), list) else [],
        "reliability": params.get("reliability"),
    }


def _parse_face_not_matched(params: Dict[str, Any]) -> Dict[str, Any]:
    obj = params.get("object") if isinstance(params.get("object"), dict) else {}
    rotation = params.get("rotation") if isinstance(params.get("rotation"), dict) else {}
    attributes = params.get("attributes") if isinstance(params.get("attributes"), dict) else {}

    return {
        "object_id": obj.get("id"),
        "bounding_box": obj.get("bounding_box") if isinstance(obj.get("bounding_box"), dict) else {},
        "rotation": {
            "yaw": rotation.get("yaw"),
            "roll": rotation.get("roll"),
            "pitch": rotation.get("pitch"),
        },
        "attributes": attributes,
        "scrfd_enabled": params.get("scrfd_enabled"),
        "face_attribute_enabled": params.get("face_attribute_enabled"),
    }


def format_alarm_caption(parsed: ParsedAlarm) -> str:
    """Короткий человекочитаемый текст для уведомлений."""
    parts: List[str] = []
    if parsed.topic:
        parts.append(parsed.topic)
    if parsed.monitor_name:
        parts.append(parsed.monitor_name)
    if parsed.channel_name:
        parts.append(parsed.channel_name)

    # topic-specific
    if parsed.topic == "PlateNotMatched":
        plate = parsed.details.get("plate_number")
        if plate:
            parts.append(f"plate={plate}")
    if parsed.topic == "FaceNotMatched":
        gender = _get_in(parsed.details, ["attributes", "gender"], None)
        age = _get_in(parsed.details, ["attributes", "age"], None)
        if gender or age:
            parts.append(f"face={gender or '?'}:{age or '?'}")
    if parsed.topic == "LineCrossed":
        best_class = parsed.details.get("best_class")
        if best_class:
            parts.append(f"obj={best_class}")

    return " | ".join(parts) or "Alarm"

