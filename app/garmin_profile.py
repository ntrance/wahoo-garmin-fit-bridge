from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from app.private_files import write_private_text
from app.settings import Settings


@dataclass(frozen=True)
class GarminProfile:
    name: str
    garmin_username: str
    garmin_password: str
    manufacturer: int
    device: int
    serial_number: int
    software_version: int | None = None


def garmin_profile_path(settings: Settings) -> Path:
    return settings.garmin_config_dir / "profile.json"


def garmin_token_dir(settings: Settings) -> Path:
    return settings.garmin_config_dir / "tokens"


def has_garmin_token_files(token_dir: Path) -> bool:
    try:
        return any(item.is_file() and not item.name.startswith(".") for item in token_dir.iterdir())
    except FileNotFoundError:
        return False


def load_garmin_profile(settings: Settings) -> GarminProfile | None:
    profile_path = garmin_profile_path(settings)
    migrate_legacy_fit_file_faker_state(settings)
    if not profile_path.exists():
        return None

    data = json.loads(profile_path.read_text(encoding="utf-8"))
    return GarminProfile(
        name=str(data.get("name") or settings.garmin_profile_name),
        garmin_username=str(data.get("garmin_username") or ""),
        garmin_password=str(data.get("garmin_password") or ""),
        manufacturer=int(data.get("manufacturer") or 1),
        device=int(data.get("device") or 0),
        serial_number=int(data.get("serial_number") or 0),
        software_version=_optional_int(data.get("software_version")),
    )


def save_garmin_profile(settings: Settings, profile: GarminProfile) -> Path:
    profile_path = garmin_profile_path(settings)
    payload = json.dumps(asdict(profile), indent=2, sort_keys=True) + "\n"
    write_private_text(profile_path, payload)
    return profile_path


def migrate_legacy_fit_file_faker_state(settings: Settings) -> bool:
    migrated = False
    legacy_root = settings.garmin_config_dir.parent / "fit-file-faker"
    legacy_config = legacy_root / "config" / "FitFileFaker" / ".config.json"
    profile = _load_legacy_profile(legacy_config, settings.garmin_profile_name)

    if profile is not None and not garmin_profile_path(settings).exists():
        save_garmin_profile(settings, profile)
        migrated = True

    profile_name = profile.name if profile is not None else settings.garmin_profile_name
    legacy_tokens = legacy_root / "data" / "FitFileFaker" / f".garmin_{_safe_name(profile_name)}"
    target_tokens = garmin_token_dir(settings)
    if legacy_tokens.is_dir() and not has_garmin_token_files(target_tokens):
        target_tokens.mkdir(parents=True, exist_ok=True)
        for source in legacy_tokens.iterdir():
            if source.is_file():
                target = target_tokens / source.name
                shutil.copy2(source, target)
                target.chmod(0o600)
                migrated = True
    return migrated


def _load_legacy_profile(path: Path, profile_name: str) -> GarminProfile | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        return None
    selected_name = str(data.get("default_profile") or profile_name)
    selected = next(
        (item for item in profiles if isinstance(item, dict) and item.get("name") == selected_name),
        None,
    )
    if selected is None:
        selected = next(
            (item for item in profiles if isinstance(item, dict) and item.get("name") == profile_name),
            None,
        )
    if selected is None and len(profiles) == 1 and isinstance(profiles[0], dict):
        selected = profiles[0]
    if selected is None:
        return None

    return GarminProfile(
        name=str(selected.get("name") or profile_name),
        garmin_username=str(selected.get("garmin_username") or ""),
        garmin_password=str(selected.get("garmin_password") or ""),
        manufacturer=int(selected.get("manufacturer") or 1),
        device=int(selected.get("device") or 0),
        serial_number=int(selected.get("serial_number") or 0),
        software_version=_optional_int(selected.get("software_version")),
    )


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
