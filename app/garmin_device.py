from __future__ import annotations

import json
import re
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app.settings import Settings
from app.private_files import write_private_text

GARMIN_PRODUCT_NAMES = {
    3121: "Garmin Edge 530",
    3291: "Garmin Fenix 6X Pro",
    3558: "Garmin Edge 1030 Plus",
    3991: "Garmin Edge 1040",
    4063: "Garmin Edge 840",
    4064: "Garmin Edge 540",
}
@dataclass(frozen=True)
class GarminDevice:
    id: str
    label: str
    manufacturer_id: int
    product_id: int
    unit_id: int
    software_version: int | None
    software_version_label: str
    garmin_product: str
    source_file: str


def scan_real_fit_devices(settings: Settings) -> list[GarminDevice]:
    devices: list[GarminDevice] = []
    for directory in (settings.real_fit_dir, settings.real_fit_upload_dir):
        if not directory.exists():
            continue
        for fit_path in sorted(directory.glob("*.fit")):
            devices.extend(extract_garmin_devices(fit_path))
    devices = dedupe_devices(devices)
    save_detected_devices(settings, devices)
    return devices


def save_uploaded_real_fit(settings: Settings, filename: str, source_file: Any) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name) or "garmin.fit"
    if not safe_name.lower().endswith(".fit"):
        safe_name += ".fit"
    settings.real_fit_upload_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.real_fit_upload_dir / safe_name
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    total_bytes = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := source_file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.max_real_fit_upload_bytes:
                    raise ValueError(
                        f"FIT upload exceeds the {settings.max_real_fit_upload_bytes}-byte limit"
                    )
                handle.write(chunk)
        destination.chmod(0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def extract_garmin_devices(path: Path) -> list[GarminDevice]:
    from garmin_fit_sdk import Decoder, Stream

    stream = Stream.from_file(str(path))
    decoder = Decoder(stream)
    messages, _errors = decoder.read()

    candidates: list[dict[str, Any]] = []
    candidates.extend(messages.get("file_id_mesgs") or [])
    candidates.extend(messages.get("device_info_mesgs") or [])

    devices: list[GarminDevice] = []
    for message in candidates:
        manufacturer_id = _manufacturer_id(message.get("manufacturer"))
        product_id = _int_or_none(message.get("product"))
        unit_id = _int_or_none(message.get("serial_number"))
        if manufacturer_id != 1 or product_id is None or unit_id is None:
            continue
        garmin_product = str(message.get("garmin_product") or product_id)
        software_raw = message.get("software_version")
        software_version = _software_version_int(software_raw)
        software_label = "" if software_raw is None else str(software_raw)
        label = _device_label(garmin_product, product_id, unit_id, software_label)
        devices.append(
            GarminDevice(
                id=f"{manufacturer_id}:{product_id}:{unit_id}",
                label=label,
                manufacturer_id=manufacturer_id,
                product_id=product_id,
                unit_id=unit_id,
                software_version=software_version,
                software_version_label=software_label,
                garmin_product=garmin_product,
                source_file=str(path),
            )
        )
    return dedupe_devices(devices)


def load_detected_devices(settings: Settings) -> list[GarminDevice]:
    if not settings.detected_devices_path.exists():
        return []
    try:
        raw = json.loads(settings.detected_devices_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [_normalize_device(GarminDevice(**item)) for item in raw if isinstance(item, dict)]


def save_detected_devices(settings: Settings, devices: list[GarminDevice]) -> None:
    write_private_text(
        settings.detected_devices_path,
        json.dumps([asdict(device) for device in devices], indent=2),
    )


def find_detected_device(settings: Settings, device_id: str) -> GarminDevice | None:
    for device in load_detected_devices(settings):
        if device.id == device_id:
            return device
    return None


def garmin_device_presets() -> list[GarminDevice]:
    return [
        GarminDevice(
            id="1:3991:3991000001",
            label="⭐ Garmin Edge 1040 (PREFERRED - Enables Watch Physio TrueUp Sync)",
            manufacturer_id=1,
            product_id=3991,
            unit_id=3991000001,
            software_version=2118,
            software_version_label="21.18",
            garmin_product="edge_1040",
            source_file="",
        ),
        GarminDevice(
            id="1:4063:4063000001",
            label="Garmin Edge 840",
            manufacturer_id=1,
            product_id=4063,
            unit_id=4063000001,
            software_version=2118,
            software_version_label="21.18",
            garmin_product="edge_840",
            source_file="",
        ),
        GarminDevice(
            id="1:4064:4064000001",
            label="Garmin Edge 540",
            manufacturer_id=1,
            product_id=4064,
            unit_id=4064000001,
            software_version=2118,
            software_version_label="21.18",
            garmin_product="edge_540",
            source_file="",
        ),
        GarminDevice(
            id="1:3558:3558000001",
            label="Garmin Edge 1030 Plus",
            manufacturer_id=1,
            product_id=3558,
            unit_id=3558000001,
            software_version=675,
            software_version_label="6.75",
            garmin_product="edge_1030_plus",
            source_file="",
        ),
        GarminDevice(
            id="1:3121:3121000001",
            label="Garmin Edge 530",
            manufacturer_id=1,
            product_id=3121,
            unit_id=3121000001,
            software_version=975,
            software_version_label="9.75",
            garmin_product="edge_530",
            source_file="",
        ),
    ]


def find_garmin_target(settings: Settings, target_id: str) -> GarminDevice | None:
    detected = find_detected_device(settings, target_id)
    if detected is not None:
        return detected
    for preset in garmin_device_presets():
        if preset.id == target_id:
            return preset
    return None


def dedupe_devices(devices: list[GarminDevice]) -> list[GarminDevice]:
    by_id: dict[str, GarminDevice] = {}
    for device in devices:
        existing = by_id.get(device.id)
        if existing is None or (not existing.software_version and device.software_version):
            by_id[device.id] = device
    return list(by_id.values())


def _manufacturer_id(value: Any) -> int | None:
    if value == "garmin":
        return 1
    return _int_or_none(value)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _software_version_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number * 100))


def _device_label(garmin_product: str, product_id: int, unit_id: int, software_label: str) -> str:
    product_name = garmin_product_display_name(garmin_product, product_id)
    details = [f"product {product_id}", f"unit {unit_id}"]
    if software_label:
        details.append(f"software {software_label}")
    return f"{product_name} - {' - '.join(details)}"


def garmin_product_display_name(garmin_product: str, product_id: int) -> str:
    if product_id in GARMIN_PRODUCT_NAMES:
        return GARMIN_PRODUCT_NAMES[product_id]
    product_name = garmin_product.replace("_", " ").replace("-", " ").strip()
    if not product_name or product_name.isdigit():
        return f"Garmin product {product_id}"
    if not product_name.lower().startswith("garmin"):
        return f"Garmin {product_name}"
    return product_name


def _normalize_device(device: GarminDevice) -> GarminDevice:
    label = _device_label(
        device.garmin_product,
        device.product_id,
        device.unit_id,
        device.software_version_label,
    )
    return replace(device, label=label)
