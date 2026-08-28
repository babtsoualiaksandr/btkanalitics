"""Скачивание видео-клипа из архива VMS для конкретного Alarm.

Цепочка (восстановлена по реальным запросам из DevTools, проверена live-пробником):

  1. POST /oauth2/v1/auth/video_stream_ticket (Bearer access_token, body: refresh_token)
     -> одноразовый ticket (JWT) для WS.
  2. wss://<host>/n7/ws/streams/archive/?since=<ISO8601>&stream_id=<id>&ticket=<ticket>
     -> бинарные WS-фреймы: первый — fMP4 init-сегмент (ftyp+moov), далее — CMAF-фрагменты
     (moof+mdat), пронумерованные по mfhd.sequence_number.

Сервер отдаёт весь запрошенный бэклог одним пакетом почти мгновенно (не в реальном
времени), поэтому останавливаться по wall-clock нельзя. Вместо этого разбираем ISO BMFF
box `tfdt` (base media decode time) внутри каждого moof/traf и timescale из mdhd
init-сегмента — так получаем точное покрытие в секундах от `since` и останавливаемся,
когда набрали нужную длительность.
"""

from __future__ import annotations

import datetime
import struct
from typing import Iterator, Optional, Tuple

import websocket

from agromash.models import AccountVideoAnalytics
from agromash.va_api_client import VAApiClient


class VideoClipError(RuntimeError):
    pass


def ms_to_iso8601(ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_video_clip_filename(alarm_id: str) -> str:
    return str(alarm_id or "unknown").replace(":", "_").replace("/", "_")[:128] + ".mp4"


def get_video_stream_ticket(client: VAApiClient, account: AccountVideoAnalytics) -> str:
    resp = client.request(
        "POST",
        "/oauth2/v1/auth/video_stream_ticket",
        headers={"Content-Type": "application/json"},
        json={"refresh_token": account.refresh_token},
    )
    if resp.status_code != 200:
        raise VideoClipError(
            f"video_stream_ticket вернул status={resp.status_code}: {resp.text[:300]}"
        )
    ticket = resp.json().get("access_token")
    if not ticket:
        raise VideoClipError("video_stream_ticket: пустой access_token в ответе")
    return ticket


# ---------------------------------------------------------------------------
# Минимальный разбор ISO BMFF (нужны только box'ы ftyp/moov/mdhd и moof/traf/tfdt)
# ---------------------------------------------------------------------------

def _iter_boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[Tuple[bytes, int, int, int]]:
    end = len(data) if end is None else end
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", data[off:off + 4])[0]
        boxtype = data[off + 4:off + 8]
        header_len = 8
        if size == 1:
            if off + 16 > end:
                break
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            header_len = 16
        elif size == 0:
            size = end - off
        if size < header_len:
            break
        yield boxtype, off, header_len, size
        off += size


def _find_path(data: bytes, path: list[bytes]) -> Optional[Tuple[int, int, int]]:
    cur_start, cur_end = 0, len(data)
    found: Optional[Tuple[int, int, int]] = None
    for boxtype in path:
        found = None
        for bt, off, header_len, size in _iter_boxes(data, cur_start, cur_end):
            if bt == boxtype:
                found = (off, header_len, size)
                cur_start, cur_end = off + header_len, off + size
                break
        if found is None:
            return None
    return found


def parse_mdhd_timescale(init_segment: bytes) -> Optional[int]:
    """Timescale трека из init-сегмента (moov/trak/mdia/mdhd)."""
    found = _find_path(init_segment, [b"moov", b"trak", b"mdia", b"mdhd"])
    if not found:
        return None
    off, header_len, size = found
    body = init_segment[off + header_len: off + size]
    if len(body) < 4:
        return None
    version = body[0]
    try:
        if version == 1:
            return struct.unpack(">I", body[20:24])[0]
        return struct.unpack(">I", body[12:16])[0]
    except struct.error:
        return None


def parse_tfdt(fragment: bytes) -> Optional[int]:
    """base_media_decode_time из moof/traf/tfdt, в единицах timescale трека."""
    found = _find_path(fragment, [b"moof", b"traf", b"tfdt"])
    if not found:
        return None
    off, header_len, size = found
    body = fragment[off + header_len: off + size]
    if len(body) < 8:
        return None
    version = body[0]
    try:
        if version == 1:
            return struct.unpack(">Q", body[4:12])[0]
        return struct.unpack(">I", body[4:8])[0]
    except struct.error:
        return None


def is_init_segment(frame: bytes) -> bool:
    return len(frame) >= 8 and frame[4:8] == b"ftyp"


def is_fragment(frame: bytes) -> bool:
    return len(frame) >= 8 and frame[4:8] == b"moof"


# ---------------------------------------------------------------------------
# Скачивание клипа
# ---------------------------------------------------------------------------

def download_archive_clip_bytes(
    *,
    base_url: str,
    stream_id: int,
    since_ms: int,
    duration_sec: float,
    ticket: str,
    connect_timeout_sec: float = 15.0,
    hard_cap_sec: float = 120.0,
    max_frames: int = 5000,
) -> bytes:
    """Скачивает клип [since_ms, since_ms + duration_sec] и возвращает готовые mp4-байты.

    Останавливается по реальному media-времени (tfdt/timescale из фреймов), а не по
    wall-clock — сервер отдаёт бэклог одним пакетом, не в реальном времени.
    """
    ws_host = base_url.replace("https://", "wss://").replace("http://", "ws://")
    since_iso = ms_to_iso8601(since_ms)
    ws_url = f"{ws_host}/n7/ws/streams/archive/?since={since_iso}&stream_id={stream_id}&ticket={ticket}"

    ws = websocket.create_connection(
        ws_url,
        timeout=connect_timeout_sec,
        header=[f"Origin: {base_url}"],
    )

    parts: list[bytes] = []
    timescale: Optional[int] = None
    first_tfdt: Optional[int] = None
    covered_sec = 0.0
    hard_cap_sec = min(max(hard_cap_sec, duration_sec * 2, 15.0), 300.0)

    try:
        import time

        deadline = time.monotonic() + hard_cap_sec
        for _ in range(max_frames):
            if time.monotonic() > deadline:
                break

            opcode, data = ws.recv_data()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode != websocket.ABNF.OPCODE_BINARY or not data:
                continue

            parts.append(data)

            if is_init_segment(data):
                timescale = parse_mdhd_timescale(data)
                continue

            if is_fragment(data) and timescale:
                tfdt = parse_tfdt(data)
                if tfdt is not None:
                    if first_tfdt is None:
                        first_tfdt = tfdt
                    covered_sec = (tfdt - first_tfdt) / timescale
                    if covered_sec >= duration_sec:
                        break
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not parts:
        raise VideoClipError("WS не вернул ни одного фрейма")

    return b"".join(parts)
