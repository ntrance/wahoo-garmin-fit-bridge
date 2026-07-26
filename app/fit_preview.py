from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
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


def build_activity_preview(activity: dict[str, object]) -> ActivityPreview:
    path = _activity_file_path(activity)
    if path is None:
        return ActivityPreview(False, "No FIT file path is stored for this activity.", None, None, [], {})
    if not path.exists():
        return ActivityPreview(False, f"FIT file is not currently available at {path}.", None, None, [], {})

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


def _route_points(records: list[Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        lat = _semicircles_to_degrees(record.get("position_lat"))
        lon = _semicircles_to_degrees(record.get("position_long"))
        if lat is None or lon is None:
            continue
        points.append((lat, lon))
    return points


def _metric_series(
    records: list[Any],
    label: str,
    unit: str,
    keys: tuple[str, ...],
    transform,
) -> dict[str, str] | None:
    samples: list[tuple[float, float]] = []
    first_time: datetime | None = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        raw = next((record[key] for key in keys if key in record), None)
        value = _to_float(raw)
        if value is None:
            continue
        value = transform(value)
        timestamp = record.get("timestamp")
        if isinstance(timestamp, datetime):
            if first_time is None:
                first_time = timestamp
            x_value = max(0.0, (timestamp - first_time).total_seconds())
        else:
            x_value = float(index)
        samples.append((x_value, value))
    if len(samples) < 2:
        return None

    values = [value for _, value in samples]
    return {
        "label": label,
        "unit": unit,
        "min": _format_number(min(values)),
        "avg": _format_number(sum(values) / len(values)),
        "max": _format_number(max(values)),
        "samples": samples,
    }


def _summary(
    records: list[Any],
    route_points: list[tuple[float, float]],
    metrics: list[dict[str, Any]],
) -> dict[str, str]:
    duration_seconds = _duration_seconds(records)
    summary = {
        "GPS points": str(len(route_points)),
        "Record samples": str(len(records)),
    }
    if duration_seconds is not None:
        summary["Duration"] = _format_duration(duration_seconds)
    for metric in metrics:
        summary[f"{metric['label']} avg"] = f"{metric['avg']} {metric['unit']}"
    return summary


def _route_svg(points: list[tuple[float, float]]) -> str:
    width = 920
    height = 320
    padding = 24
    sampled = _sample(points, 650)
    zoom = _route_zoom(sampled, width, height, padding)
    projected = [_lat_lon_to_world(lat, lon, zoom) for lat, lon in sampled]
    min_x = min(x for x, _ in projected)
    max_x = max(x for x, _ in projected)
    min_y = min(y for _, y in projected)
    max_y = max(y for _, y in projected)
    center_x = (min_x + max_x) / 2
    center_y = (min_y + max_y) / 2
    left = center_x - width / 2
    top = center_y - height / 2

    coords: list[str] = []
    for x_world, y_world in projected:
        coords.append(f"{x_world - left:.1f},{y_world - top:.1f}")

    tiles = _map_tiles(left, top, width, height, zoom)
    tile_images = "".join(
        (
            f'<image href="https://tile.openstreetmap.org/{zoom}/{tile_x}/{tile_y}.png" '
            f'x="{x:.1f}" y="{y:.1f}" width="256" height="256" preserveAspectRatio="none"/>'
        )
        for tile_x, tile_y, x, y in tiles
    )
    first_x, first_y = coords[0].split(",")
    last_x, last_y = coords[-1].split(",")

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Route map preview" '
        'class="w-100 border rounded bg-light">'
        '<defs><filter id="route-shadow" x="-10%" y="-10%" width="120%" height="120%">'
        '<feDropShadow dx="0" dy="1" stdDeviation="1.2" flood-color="#000" flood-opacity="0.45"/>'
        "</filter></defs>"
        '<rect x="0" y="0" width="920" height="320" fill="#e9ecef"/>'
        f"{tile_images}"
        '<rect x="0" y="0" width="920" height="320" fill="none" stroke="#ced4da"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="#ffffff" stroke-width="7" '
        'stroke-linecap="round" stroke-linejoin="round" opacity="0.9" filter="url(#route-shadow)"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="#0d6efd" stroke-width="4" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{first_x}" cy="{first_y}" r="6" fill="#198754" stroke="#fff" stroke-width="2"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="6" fill="#dc3545" stroke="#fff" stroke-width="2"/>'
        '<rect x="672" y="292" width="238" height="20" fill="#ffffff" opacity="0.85" rx="3"/>'
        '<text x="902" y="307" text-anchor="end" fill="#495057" font-size="11">'
        "© OpenStreetMap contributors</text>"
        "</svg>"
    )


def _route_zoom(points: list[tuple[float, float]], width: int, height: int, padding: int) -> int:
    for zoom in range(18, 2, -1):
        projected = [_lat_lon_to_world(lat, lon, zoom) for lat, lon in points]
        route_width = max(x for x, _ in projected) - min(x for x, _ in projected)
        route_height = max(y for _, y in projected) - min(y for _, y in projected)
        if route_width <= width - padding * 2 and route_height <= height - padding * 2:
            return zoom
    return 3


def _lat_lon_to_world(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    sin_lat = math.sin(math.radians(lat))
    scale = 256 * (2**zoom)
    x = (lon + 180.0) / 360.0 * scale
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


def _map_tiles(
    left: float,
    top: float,
    width: int,
    height: int,
    zoom: int,
) -> list[tuple[int, int, float, float]]:
    max_tile = 2**zoom
    min_tile_x = math.floor(left / 256)
    max_tile_x = math.floor((left + width) / 256)
    min_tile_y = max(0, math.floor(top / 256))
    max_tile_y = min(max_tile - 1, math.floor((top + height) / 256))
    tiles: list[tuple[int, int, float, float]] = []
    for tile_x in range(min_tile_x, max_tile_x + 1):
        wrapped_x = tile_x % max_tile
        for tile_y in range(min_tile_y, max_tile_y + 1):
            x = tile_x * 256 - left
            y = tile_y * 256 - top
            tiles.append((wrapped_x, tile_y, x, y))
    return tiles


def _stacked_chart_svg(metrics: list[dict[str, Any]], elevation: dict[str, Any] | None) -> str:
    width = 920
    band_height = 92
    band_gap = 20
    padding_left = 44
    padding_right = 18
    padding_top = 34
    padding_bottom = 24
    height = padding_top + len(metrics) * band_height + max(0, len(metrics) - 1) * band_gap + padding_bottom
    usable_width = width - padding_left - padding_right
    all_x_values = [
        x
        for metric in metrics + ([elevation] if elevation is not None else [])
        for x, _ in metric["samples"]
    ]
    min_x = min(all_x_values)
    max_x = max(all_x_values)
    x_range = max(max_x - min_x, 0.000001)
    metric_colors = {
        "Speed": "#0d6efd",
        "Power": "#d63384",
        "Heart rate": "#dc3545",
    }
    elevation_samples = elevation["samples"] if elevation is not None else []
    elevation_area = ""

    rows: list[str] = []
    for index, metric in enumerate(metrics):
        top = padding_top + index * (band_height + band_gap)
        bottom = top + band_height
        rows.append(
            f'<path d="M{padding_left} {top:.1f} H{width - padding_right} M{padding_left} {(top + bottom) / 2:.1f} '
            f'H{width - padding_right} M{padding_left} {bottom:.1f} H{width - padding_right}" '
            'stroke="#dee2e6" stroke-width="1"/>'
        )
        if elevation_samples:
            elevation_area = _series_area_path(
                elevation_samples,
                min_x,
                x_range,
                padding_left,
                usable_width,
                top,
                bottom,
            )
            rows.append(f'<path d="{elevation_area}" fill="#198754" opacity="0.16"/>')
            rows.append(
                f'<path d="{_series_line_path(elevation_samples, min_x, x_range, padding_left, usable_width, top, bottom)}" '
                'fill="none" stroke="#198754" stroke-width="1.5" opacity="0.35"/>'
            )

        line_path = _series_line_path(
            metric["samples"],
            min_x,
            x_range,
            padding_left,
            usable_width,
            top,
            bottom,
        )
        color = metric_colors.get(str(metric["label"]), "#6f42c1")
        safe_label = escape(str(metric["label"]))
        safe_unit = escape(str(metric["unit"]))
        rows.append(
            f'<path d="{line_path}" fill="none" stroke="{color}" stroke-width="2.4" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        rows.append(f'<text x="8" y="{top + 15:.1f}" fill="#212529" font-size="13" font-weight="700">{safe_label}</text>')
        rows.append(
            f'<text x="8" y="{bottom - 4:.1f}" fill="#6c757d" font-size="11">'
            f'{escape(str(metric["min"]))}-{escape(str(metric["max"]))} {safe_unit}</text>'
        )
        rows.append(
            f'<text x="{width - padding_right}" y="{top + 15:.1f}" text-anchor="end" fill="#6c757d" font-size="12">'
            f'min {escape(str(metric["min"]))} {safe_unit} · avg {escape(str(metric["avg"]))} {safe_unit} · '
            f'max {escape(str(metric["max"]))} {safe_unit}</text>'
        )

    elevation_label = "Elevation background"
    if elevation is not None:
        elevation_label = (
            f"Elevation background: min {escape(str(elevation['min']))} {escape(str(elevation['unit']))} · "
            f"max {escape(str(elevation['max']))} {escape(str(elevation['unit']))}"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Speed power heart rate stacked chart" '
        'class="w-100 border rounded bg-light">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc"/>'
        f'<text x="{padding_left}" y="18" fill="#495057" font-size="13">{elevation_label}</text>'
        f'{"".join(rows)}'
        "</svg>"
    )


def _series_line_path(
    samples: list[tuple[float, float]],
    min_x: float,
    x_range: float,
    padding_left: int,
    usable_width: int,
    top: float,
    bottom: float,
) -> str:
    sampled = _sample(samples, 650)
    y_values = [y for _, y in sampled]
    min_y, max_y = min(y_values), max(y_values)
    y_range = max(max_y - min_y, 0.000001)
    coords = []
    for x_value, y_value in sampled:
        x = padding_left + ((x_value - min_x) / x_range) * usable_width
        y = top + ((max_y - y_value) / y_range) * (bottom - top)
        coords.append((x, y))
    head = f"M{coords[0][0]:.1f} {coords[0][1]:.1f}"
    tail = " ".join(f"L{x:.1f} {y:.1f}" for x, y in coords[1:])
    return f"{head} {tail}"


def _series_area_path(
    samples: list[tuple[float, float]],
    min_x: float,
    x_range: float,
    padding_left: int,
    usable_width: int,
    top: float,
    bottom: float,
) -> str:
    line_path = _series_line_path(samples, min_x, x_range, padding_left, usable_width, top, bottom)
    sampled = _sample(samples, 650)
    first_x = padding_left + ((sampled[0][0] - min_x) / x_range) * usable_width
    last_x = padding_left + ((sampled[-1][0] - min_x) / x_range) * usable_width
    return f"{line_path} L{last_x:.1f} {bottom:.1f} L{first_x:.1f} {bottom:.1f} Z"


def _sample(values: list[Any], max_points: int) -> list[Any]:
    if len(values) <= max_points:
        return values
    step = len(values) / max_points
    sampled = [values[int(index * step)] for index in range(max_points)]
    if sampled[-1] != values[-1]:
        sampled[-1] = values[-1]
    return sampled


def _duration_seconds(records: list[Any]) -> float | None:
    times = [
        record.get("timestamp")
        for record in records
        if isinstance(record, dict) and isinstance(record.get("timestamp"), datetime)
    ]
    if len(times) < 2:
        return None
    return max(0.0, (max(times) - min(times)).total_seconds())


def _semicircles_to_degrees(value: Any) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    return numeric * (180.0 / 2**31)


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
