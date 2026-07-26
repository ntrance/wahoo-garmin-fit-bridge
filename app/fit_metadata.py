from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FitMetadata:
    sha256: str
    file_size: int
    activity_start_time: str | None
    total_distance_meters: float | None


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_fit_summary(path: Path) -> tuple[str | None, float | None]:
    try:
        from garmin_fit_sdk import Decoder, Stream

        stream = Stream.from_file(str(path))
        decoder = Decoder(stream)
        messages, errors = decoder.read()
        if errors:
            logger.debug("FIT decoder reported errors for %s: %s", path, errors)
        return _find_datetime(messages), _find_total_distance(messages)
    except Exception as exc:  # pragma: no cover - exact decoder failures vary by file
        logger.warning("Could not extract FIT metadata from %s: %s", path, exc)
        return None, None


def extract_activity_start_time(path: Path) -> str | None:
    start_time, _ = extract_fit_summary(path)
    return start_time


def compute_fit_metadata(path: Path) -> FitMetadata:
    activity_start_time, total_distance_meters = extract_fit_summary(path)
    return FitMetadata(
        sha256=calculate_sha256(path),
        file_size=path.stat().st_size,
        activity_start_time=activity_start_time,
        total_distance_meters=total_distance_meters,
    )


def _find_datetime(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("start_time", "time_created", "timestamp"):
            if key in value:
                converted = _to_iso(value[key])
                if converted is not None:
                    return converted
        for child in value.values():
            converted = _find_datetime(child)
            if converted is not None:
                return converted
    elif isinstance(value, list):
        for child in value:
            converted = _find_datetime(child)
            if converted is not None:
                return converted
    return None


def _find_total_distance(value: Any) -> float | None:
    if isinstance(value, dict):
        session_distance = _first_distance(value.get("session_mesgs"))
        if session_distance is not None:
            return session_distance
        lap_distance = _first_distance(value.get("lap_mesgs"))
        if lap_distance is not None:
            return lap_distance
        record_distance = _last_record_distance(value.get("record_mesgs"))
        if record_distance is not None:
            return record_distance
        if "total_distance" in value:
            return _to_float(value["total_distance"])
        for child in value.values():
            distance = _find_total_distance(child)
            if distance is not None:
                return distance
    elif isinstance(value, list):
        for child in value:
            distance = _find_total_distance(child)
            if distance is not None:
                return distance
    return None


def _first_distance(value: Any) -> float | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "total_distance" in item:
                distance = _to_float(item["total_distance"])
                if distance is not None:
                    return distance
    return None


def _last_record_distance(value: Any) -> float | None:
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict) and "distance" in item:
                distance = _to_float(item["distance"])
                if distance is not None:
                    return distance
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    return None
