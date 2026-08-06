from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from html import escape
import json
import math
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ActivityPreview:
    available: bool
    message: str
    route_svg: str | None
    stacked_chart_svg: str | None
    metrics: list[dict[str, Any]]
    summary: dict[str, str]


def get_disk_preview_path(activity_id: str | int, previews_dir: Path | None = None) -> Path | None:
    if not activity_id:
        return None
    base_dir = previews_dir or Path("/data/previews")
    return base_dir / f"{activity_id}.json"


def save_preview_to_disk(activity_id: str | int, preview: ActivityPreview, previews_dir: Path | None = None) -> None:
    path = get_disk_preview_path(activity_id, previews_dir)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "available": preview.available,
            "message": preview.message,
            "route_svg": preview.route_svg,
            "stacked_chart_svg": preview.stacked_chart_svg,
            "metrics": preview.metrics,
            "summary": preview.summary,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_preview_from_disk(activity_id: str | int, previews_dir: Path | None = None) -> ActivityPreview | None:
    path = get_disk_preview_path(activity_id, previews_dir)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ActivityPreview(
            available=data.get("available", False),
            message=data.get("message", ""),
            route_svg=data.get("route_svg"),
            stacked_chart_svg=data.get("stacked_chart_svg"),
            metrics=data.get("metrics", []),
            summary=data.get("summary", {}),
        )
    except Exception:
        return None


def build_activity_preview(activity: dict[str, object], previews_dir: Path | None = None) -> ActivityPreview:
    activity_id = activity.get("id") or activity.get("activity_id")
    if activity_id:
        cached = load_preview_from_disk(activity_id, previews_dir)
        if cached is not None:
            return cached

    path = _activity_file_path(activity)
    if path is None:
        return ActivityPreview(False, "No FIT file path is stored for this activity.", None, None, [], {})
    if not path.exists():
        return ActivityPreview(False, f"FIT file is not currently available at {path}.", None, None, [], {})

    try:
        stat = path.stat()
        preview = _build_activity_preview_cached(str(path.resolve()), stat.st_mtime, stat.st_size)
        if activity_id and preview.available:
            save_preview_to_disk(activity_id, preview, previews_dir)
        return preview
    except Exception as exc:  # pragma: no cover - decoder errors vary by FIT file
        return ActivityPreview(False, f"Could not read FIT preview data: {exc}", None, None, [], {})


def prebuild_activity_preview(activity: dict[str, object], previews_dir: Path | None = None) -> ActivityPreview:
    activity_id = activity.get("id") or activity.get("activity_id")
    path = _activity_file_path(activity)
    if path is None or not path.exists():
        return ActivityPreview(False, "FIT file unavailable.", None, None, [], {})
    try:
        stat = path.stat()
        preview = _build_activity_preview_cached(str(path.resolve()), stat.st_mtime, stat.st_size)
        if activity_id:
            save_preview_to_disk(activity_id, preview, previews_dir)
        return preview
    except Exception as exc:
        return ActivityPreview(False, f"Could not prebuild FIT preview: {exc}", None, None, [], {})


@lru_cache(maxsize=128)
def _build_activity_preview_cached(path_str: str, mtime: float, size: int) -> ActivityPreview:
    path = Path(path_str)
    try:
        from garmin_fit_sdk import Decoder, Stream

        messages, errors = Decoder(Stream.from_file(str(path))).read()
    except Exception as exc:  # pragma: no cover - decoder errors vary by FIT file
        return ActivityPreview(False, f"Could not read FIT preview data: {exc}", None, None, [], {})

    records = messages.get("record_mesgs", [])
    if not isinstance(records, list) or not records:
        return ActivityPreview(False, "No record stream was found in this FIT file.", None, None, [], {})

    route_points = _route_points(records)
    route_svg = _route_svg(route_points) if len(route_points) >= 2 else None
    metrics = [
        metric
        for metric in (
            _metric_series(records, "Speed", "kph", ("enhanced_speed", "speed"), lambda value: value * 3.6),
            _metric_series(records, "Power", "W", ("power",), lambda value: value),
            _metric_series(records, "Heart rate", "bpm", ("heart_rate",), lambda value: value),
        )
        if metric is not None
    ]
    elevation = _metric_series(records, "Elevation", "m", ("enhanced_altitude", "altitude"), lambda value: value)
    stacked_chart_svg = _stacked_chart_svg(metrics, elevation) if metrics else None
    summary = _summary(records, route_points, metrics)
    warnings = ""
    if errors:
        warnings = " FIT decoder reported warnings; preview may be incomplete."
    if route_svg is None and not metrics:
        return ActivityPreview(False, "No GPS, speed, power, or heart-rate samples were found.", None, None, [], summary)
    return ActivityPreview(True, warnings.strip(), route_svg, stacked_chart_svg, metrics, summary)


def _activity_file_path(activity: dict[str, object]) -> Path | None:
    for key in ("current_path", "source_path"):
        value = activity.get(key)
        if value:
            return Path(str(value))
    return None


def _route_points(records: list[dict[str, Any]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    step = max(1, len(records) // 650)
    for index in range(0, len(records), step):
        record = records[index]
        lat = record.get("position_lat")
        lon = record.get("position_long")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            lat_deg = lat * (180.0 / (2**31))
            lon_deg = lon * (180.0 / (2**31))
            if -90.0 <= lat_deg <= 90.0 and -180.0 <= lon_deg <= 180.0:
                points.append((lat_deg, lon_deg))
    return points


def _route_svg(points: list[tuple[float, float]]) -> str:
    min_lat = min(lat for lat, _ in points)
    max_lat = max(lat for lat, _ in points)
    min_lon = min(lon for _, lon in points)
    max_lon = max(lon for _, lon in points)

    mean_lat = math.radians((min_lat + max_lat) / 2.0)
    cos_lat = max(math.cos(mean_lat), 0.2)

    coords: list[tuple[float, float]] = []
    for lat, lon in points:
        x = (lon - min_lon) * cos_lat
        y = max_lat - lat
        coords.append((x, y))

    min_x = min(x for x, _ in coords)
    max_x = max(x for x, _ in coords)
    min_y = min(y for _, y in coords)
    max_y = max(y for _, y in coords)

    width = max_x - min_x
    height = max_y - min_y

    if width <= 0 or height <= 0:
        return ""

    padding = max(width, height) * 0.08
    view_min_x = min_x - padding
    view_min_y = min_y - padding
    view_width = width + (padding * 2)
    view_height = height + (padding * 2)

    path_d = "M " + " L ".join(f"{x:.6f} {y:.6f}" for x, y in coords)
    start_x, start_y = coords[0]
    end_x, end_y = coords[-1]
    stroke_width = max(view_width, view_height) / 120.0
    circle_radius = stroke_width * 1.5

    return f"""<svg viewBox="{view_min_x:.6f} {view_min_y:.6f} {view_width:.6f} {view_height:.6f}"
     width="100%" height="240" preserveAspectRatio="xMidYMid meet"
     xmlns="http://www.w3.org/2000/svg" class="img-fluid rounded border bg-light">
  <path d="{path_d}" fill="none" stroke="#0d6efd" stroke-width="{stroke_width:.6f}" stroke-linecap="round" stroke-linejoin="round" />
  <circle cx="{start_x:.6f}" cy="{start_y:.6f}" r="{circle_radius:.6f}" fill="#198754" />
  <circle cx="{end_x:.6f}" cy="{end_y:.6f}" r="{circle_radius:.6f}" fill="#dc3545" />
</svg>"""


def _metric_series(
    records: list[dict[str, Any]],
    label: str,
    unit: str,
    fields: tuple[str, ...],
    convert_fn: Any,
) -> dict[str, Any] | None:
    series: list[tuple[datetime, float]] = []
    raw_values: list[float] = []

    step = max(1, len(records) // 350)
    for index in range(0, len(records), step):
        record = records[index]
        value = None
        for field in fields:
            if field in record and record[field] is not None:
                value = record[field]
                break

        timestamp = record.get("timestamp")
        if value is not None and isinstance(timestamp, datetime):
            try:
                converted = float(convert_fn(value))
                series.append((timestamp, converted))
                raw_values.append(converted)
            except (ValueError, TypeError):
                pass

    if not series or not raw_values:
        return None

    avg_val = sum(raw_values) / len(raw_values)
    max_val = max(raw_values)

    return {
        "label": label,
        "unit": unit,
        "series": series,
        "avg": f"{avg_val:.1f} {unit}",
        "max": f"{max_val:.1f} {unit}",
    }


def _stacked_chart_svg(
    metrics: list[dict[str, Any]],
    elevation: dict[str, Any] | None,
) -> str:
    color_map = {
        "Speed": "#0d6efd",
        "Power": "#fd7e14",
        "Heart rate": "#dc3545",
        "Elevation": "#6c757d",
    }

    svg_parts: list[str] = [
        '<svg viewBox="0 0 800 240" width="100%" height="240" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg" class="rounded border bg-light p-2">'
    ]

    all_series = metrics + ([elevation] if elevation else [])
    for metric in all_series:
        if not metric or "series" not in metric:
            continue

        label = metric["label"]
        series = metric["series"]
        if len(series) < 2:
            continue

        color = color_map.get(label, "#212529")
        min_v = min(v for _, v in series)
        max_v = max(v for _, v in series)
        span_v = max(max_v - min_v, 1.0)

        coords: list[tuple[float, float]] = []
        num_points = len(series)

        for i, (_, val) in enumerate(series):
            x = (i / (num_points - 1)) * 760.0 + 20.0
            norm_y = (val - min_v) / span_v
            y = 210.0 - (norm_y * 170.0)
            coords.append((x, y))

        path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
        opacity = "0.4" if label == "Elevation" else "0.8"
        stroke_w = "1.5" if label == "Elevation" else "2.0"

        svg_parts.append(
            f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="{stroke_w}" stroke-opacity="{opacity}" stroke-linecap="round" />'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


def _summary(
    records: list[dict[str, Any]],
    route_points: list[tuple[float, float]],
    metrics: list[dict[str, Any]],
) -> dict[str, str]:
    summary_data: dict[str, str] = {}

    timestamps = [r.get("timestamp") for r in records if isinstance(r.get("timestamp"), datetime)]
    if timestamps:
        start = min(timestamps)
        end = max(timestamps)
        duration_sec = int((end - start).total_seconds())
        hours, remainder = divmod(duration_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            summary_data["Duration"] = f"{hours}h {minutes}m {seconds}s"
        else:
            summary_data["Duration"] = f"{minutes}m {seconds}s"

    summary_data["GPS Track Points"] = str(len(route_points))

    for m in metrics:
        summary_data[f"Avg {m['label']}"] = m["avg"]
        summary_data[f"Max {m['label']}"] = m["max"]

    return summary_data
