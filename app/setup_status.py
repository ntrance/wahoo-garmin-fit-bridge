from __future__ import annotations

import os
import json
import re
import shlex
import shutil
# Commands use fixed argv and never enable shell execution.
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app.garmin_profile import (
    GarminProfile,
    garmin_profile_path,
    garmin_token_dir,
    has_garmin_token_files,
    load_garmin_profile,
    save_garmin_profile as write_garmin_profile,
)
from app.garmin_upload import create_garmin_session
from app.garmin_guard import (
    active_garmin_cooldown,
    clear_garmin_cooldown,
)
from app.settings import Settings
from app.private_files import write_private_text


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    title: str
    output: str


def build_setup_status(settings: Settings) -> dict[str, object]:
    rclone_conf = settings.rclone_config_path
    rclone_text = _safe_read(rclone_conf)
    remote_marker = f"[{settings.rclone_remote}]"
    garmin_profile = load_garmin_profile_status(settings)
    garmin_profile["detected_device_id"] = _matched_detected_device_id(settings, garmin_profile)
    garmin_profile["cooldown"] = active_garmin_cooldown(settings)

    return {
        "dropbox": {
            "configured": rclone_conf.exists() and remote_marker in rclone_text,
            "config_path": rclone_conf,
            "remote": settings.rclone_remote,
            "path": settings.dropbox_wahoo_path,
            "command": "bash scripts/setup-rclone.sh",
            "discover_command": "bash scripts/discover-dropbox-paths.sh",
        },
        "garmin": {
            "configured": garmin_profile["configured"],
            "config_path": garmin_profile_path(settings),
            "profile": garmin_profile["profile"],
            "unit_id_configured": bool(garmin_profile["unit_id"]),
            "device": garmin_profile["device_name"],
            **garmin_profile,
        },
        "runtime": {
            "dry_run": settings.dry_run,
            "runtime_config_path": settings.runtime_config_path,
            "restart_command": "docker compose up -d --build",
        },
    }


def load_garmin_profile_status(settings: Settings) -> dict[str, object]:
    status: dict[str, object] = {
        "configured": False,
        "profile": settings.garmin_profile_name,
        "username": "",
        "password_saved": False,  # nosec B105
        "manufacturer_id": "1",
        "product_id": "",
        "unit_id": settings.garmin_unit_id,
        "software_version": "",
        "device_name": settings.garmin_device_name,
        "device_target_name": settings.garmin_device_name,
        "device_target_summary": settings.garmin_device_name,
        "matched_detected_device_label": "",
        "profile_config_path": "",
        "default_profile": "",
        "detected_device_id": "",
        "token_dir": "",
        "token_saved": False,  # nosec B105
        "profile_error": "",
    }

    try:
        profile = load_garmin_profile(settings)
    except Exception as exc:
        status["profile_error"] = f"Could not read Garmin upload profile: {exc}"
        return status
    if profile is None:
        return status

    status["profile_config_path"] = garmin_profile_path(settings)
    status["default_profile"] = profile.name
    manufacturer_id = _string_or_empty(profile.manufacturer)
    product_id = _string_or_empty(profile.device)
    unit_id = _string_or_empty(profile.serial_number) or settings.garmin_unit_id
    software_version = _string_or_empty(profile.software_version)
    token_dir = garmin_token_dir(settings)
    matched_device = _matched_detected_device(settings, product_id, unit_id)
    device_target_name = settings.garmin_device_name
    software_label = _software_version_display(software_version)
    matched_detected_device_label = ""
    if matched_device is not None:
        try:
            from app.garmin_device import garmin_product_display_name
        except Exception:  # nosec B110
            pass
        else:
            device_target_name = garmin_product_display_name(matched_device.garmin_product, matched_device.product_id)
        software_label = matched_device.software_version_label or software_label
        matched_detected_device_label = matched_device.label
    else:
        try:
            from app.garmin_device import garmin_product_display_name
        except Exception:  # nosec B110
            pass
        else:
            try:
                device_target_name = garmin_product_display_name("", int(product_id))
            except ValueError:
                pass
    device_target_summary = _device_summary(device_target_name, product_id, unit_id, software_label)

    status.update(
        {
            "configured": bool(
                profile.garmin_username
                and profile.garmin_password
                and product_id
                and unit_id
            ),
            "profile": profile.name,
            "username": profile.garmin_username,
            "password_saved": bool(profile.garmin_password),
            "manufacturer_id": manufacturer_id or "1",
            "product_id": product_id,
            "unit_id": unit_id,
            "software_version": software_version,
            "device_name": device_target_summary,
            "device_target_name": device_target_name,
            "device_target_summary": device_target_summary,
            "matched_detected_device_label": matched_detected_device_label,
            "token_dir": token_dir,
            "token_saved": has_garmin_token_files(token_dir),
        }
    )
    return status


def save_runtime_config(settings: Settings, updates: dict[str, str]) -> None:
    settings.runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _parse_env(settings.runtime_config_path)
    existing.update(updates)
    allowed_keys = [
        "RCLONE_REMOTE",
        "DROPBOX_WAHOO_PATH",
        "GARMIN_PROFILE_NAME",
        "GARMIN_DEVICE_NAME",
        "GARMIN_UNIT_ID",
        "DRY_RUN",
        "DROPBOX_SOURCE_ENABLED",
        "IGPSPORT_SOURCE_ENABLED",
        "DROPBOX_POLL_SECONDS",
        "IGPSPORT_POLL_SECONDS",
    ]
    lines = [f"{key}={_quote_env(existing.get(key, ''))}" for key in allowed_keys]
    write_private_text(settings.runtime_config_path, "\n".join(lines) + "\n")
    os.environ.update({key: existing.get(key, "") for key in allowed_keys})


def save_dropbox_auth(
    settings: Settings,
    *,
    remote_name: str,
    token_json: str = "",
    full_config: str = "",
) -> CommandResult:
    remote_name = remote_name.strip() or settings.rclone_remote
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)

    if full_config.strip():
        config_text = full_config.strip() + "\n"
        if f"[{remote_name}]" not in config_text:
            return CommandResult(
                False,
                "Dropbox setup",
                f"The pasted rclone config does not contain a [{remote_name}] remote.",
            )
        write_private_text(settings.rclone_config_path, config_text)
        return CommandResult(True, "Dropbox setup", f"Saved rclone config for [{remote_name}].")

    if not token_json.strip():
        return CommandResult(False, "Dropbox setup", "Paste either a Dropbox token JSON or a complete rclone config.")

    try:
        token = json.loads(token_json)
    except json.JSONDecodeError as exc:
        return CommandResult(False, "Dropbox setup", f"Dropbox token JSON is invalid: {exc}")

    if not isinstance(token, dict) or "access_token" not in token:
        return CommandResult(False, "Dropbox setup", "Dropbox token JSON must include access_token.")

    compact_token = json.dumps(token, separators=(",", ":"))
    config_text = f"[{remote_name}]\ntype = dropbox\ntoken = {compact_token}\n"
    write_private_text(settings.rclone_config_path, config_text)
    return CommandResult(True, "Dropbox setup", f"Saved Dropbox rclone remote [{remote_name}].")


def save_garmin_profile(
    settings: Settings,
    *,
    profile_name: str,
    garmin_username: str,
    garmin_password: str,
    manufacturer: str,
    product_id: str,
    unit_id: str,
    software_version: str = "",
) -> CommandResult:
    if not profile_name.strip():
        return CommandResult(False, "Garmin setup", "Profile name is required.")
    if not garmin_username.strip():
        return CommandResult(False, "Garmin setup", "Garmin username/email is required.")
    if not unit_id.strip():
        return CommandResult(False, "Garmin setup", "Garmin Unit ID is required.")
    if not product_id.strip():
        return CommandResult(False, "Garmin setup", "Garmin product ID is required.")

    try:
        manufacturer_int = int(manufacturer.strip() or "1")
        product_int = int(product_id.strip())
        unit_id_int = int(unit_id.strip())
        software_version_int = int(software_version.strip()) if software_version.strip() else None
    except ValueError:
        return CommandResult(False, "Garmin setup", "Manufacturer, product ID, Unit ID, and software version must be numbers.")

    try:
        existing = load_garmin_profile(settings)
    except Exception as exc:
        return CommandResult(False, "Garmin setup", f"Could not read Garmin profile: {exc}")

    try:
        password = garmin_password or (existing.garmin_password if existing is not None else "")
        if not password:
            return CommandResult(
                False,
                "Garmin setup",
                "Garmin password is required when creating a new profile.",
            )
        profile = GarminProfile(
            name=profile_name,
            garmin_username=garmin_username,
            garmin_password=password,
            manufacturer=manufacturer_int,
            device=product_int,
            serial_number=unit_id_int,
            software_version=software_version_int,
        )
        config_path = write_garmin_profile(settings, profile)
    except Exception as exc:
        return CommandResult(False, "Garmin setup", f"Could not save Garmin profile: {exc}")

    return CommandResult(
        True,
        "Garmin setup",
        f"Saved Garmin upload profile at {config_path}. Garmin password was not displayed.",
    )


def clear_garmin_session_pause(settings: Settings) -> CommandResult:
    cleared = clear_garmin_cooldown(settings)
    if cleared:
        return CommandResult(
            True,
            "Garmin upload pause cleared",
            "Cleared the local Garmin login pause. The next bridge upload can try again.",
        )
    return CommandResult(
        True,
        "Garmin upload pause",
        "There was no active local Garmin login pause.",
    )


def create_garmin_session_token(settings: Settings) -> CommandResult:
    result = create_garmin_session(settings)
    return CommandResult(result.success, "Garmin session", result.combined_output)


def test_dropbox(settings: Settings) -> CommandResult:
    if shutil.which("rclone") is None:
        return CommandResult(False, "Dropbox test", "rclone is not installed in this container image.")
    if not settings.rclone_config_path.exists():
        return CommandResult(False, "Dropbox test", f"rclone config not found at {settings.rclone_config_path}.")

    command = [
        "rclone",
        "lsf",
        f"{settings.rclone_remote}:{settings.dropbox_wahoo_path}",
        "--max-depth",
        "1",
        "--config",
        str(settings.rclone_config_path),
    ]
    return _run(command, "Dropbox test")


def sync_dropbox_to_incoming(settings: Settings) -> CommandResult:
    if shutil.which("rclone") is None:
        return CommandResult(False, "Dropbox sync", "rclone is not installed in this container image.")
    if not settings.rclone_config_path.exists():
        return CommandResult(False, "Dropbox sync", f"rclone config not found at {settings.rclone_config_path}.")

    settings.incoming_dir.mkdir(parents=True, exist_ok=True)
    remote = f"{settings.rclone_remote}:{settings.dropbox_wahoo_path}"
    list_command = [
        "rclone",
        "lsjson",
        remote,
        "--files-only",
        "--max-depth",
        "1",
        "--include",
        "*.fit",
        "--config",
        str(settings.rclone_config_path),
    ]

    try:
        listed = subprocess.run(  # nosec B603
            list_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(False, "Dropbox sync", "Timed out while listing Dropbox FIT files.")

    list_output = "\n".join(part for part in (listed.stdout, listed.stderr) if part).strip()
    if listed.returncode != 0:
        return CommandResult(False, "Dropbox sync", list_output or "rclone listing failed.")

    try:
        entries = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return CommandResult(False, "Dropbox sync", "rclone returned an invalid Dropbox file listing.")
    if not isinstance(entries, list):
        return CommandResult(False, "Dropbox sync", "rclone returned an invalid Dropbox file listing.")

    db = Database(settings.sqlite_path)
    db.init()
    known = db.list_file_fingerprints()
    canonical_known = {
        (_canonical_fit_filename(name), size)
        for name, size in known
    }
    pending: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("Name") or "")
        size = entry.get("Size")
        if (
            not name
            or name != Path(name).name
            or "/" in name
            or "\\" in name
            or "\n" in name
            or not name.lower().endswith(".fit")
            or not isinstance(size, int)
        ):
            continue
        fingerprint = (_canonical_fit_filename(name), size)
        if fingerprint not in canonical_known and not (settings.incoming_dir / name).exists():
            pending.append(name)

    copied: list[str] = []
    copy_output = ""
    if pending:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as files_from:
            files_from.write("\n".join(pending) + "\n")
            files_from.flush()
            command = [
                "rclone",
                "copy",
                remote,
                str(settings.incoming_dir),
                "--files-from-raw",
                files_from.name,
                "--ignore-existing",
                "--config",
                str(settings.rclone_config_path),
            ]
            try:
                completed = subprocess.run(  # nosec B603
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                return CommandResult(False, "Dropbox sync", "Timed out while copying from Dropbox.")

        copy_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        if completed.returncode != 0:
            return CommandResult(False, "Dropbox sync", copy_output or "rclone copy failed.")
        copied = sorted(name for name in pending if (settings.incoming_dir / name).is_file())

    status_line = (
        f"{datetime.now(timezone.utc).isoformat()} copied {len(copied)} new FIT file(s) "
        f"from {remote}; skipped {len(entries) - len(pending)} already handled file(s)."
    )
    if copied:
        status_line += " " + ", ".join(copied[:10])
    marker = settings.incoming_dir.parent / ".last-rclone-copy"
    marker.write_text(status_line + ("\n" + copy_output if copy_output else "") + "\n")
    return CommandResult(True, "Dropbox sync", status_line)


def _canonical_fit_filename(filename: str) -> str:
    return re.sub(r"[\s_]+", "_", filename.strip()).casefold()


def delete_dropbox_source(settings: Settings, filename: str) -> CommandResult:
    title = "Dropbox delete"
    if shutil.which("rclone") is None:
        return CommandResult(False, title, "rclone is not installed in this container image.")
    if not settings.rclone_config_path.exists():
        return CommandResult(False, title, f"rclone config not found at {settings.rclone_config_path}.")
    if not filename or filename != Path(filename).name or "/" in filename or "\\" in filename:
        return CommandResult(False, title, "Refusing to delete an unsafe Dropbox filename.")
    if not filename.lower().endswith(".fit"):
        return CommandResult(False, title, "Only FIT files can be deleted from Dropbox.")

    dropbox_path = settings.dropbox_wahoo_path.strip("/")
    remote_path = f"{settings.rclone_remote}:{filename}"
    if dropbox_path:
        remote_path = f"{settings.rclone_remote}:{dropbox_path}/{filename}"
    command = [
        "rclone",
        "deletefile",
        remote_path,
        "--config",
        str(settings.rclone_config_path),
    ]
    result = _run(command, title)
    if not result.ok:
        return result
    return CommandResult(True, title, f"Deleted {remote_path} from Dropbox.\n{result.output}")


def test_garmin_upload(settings: Settings) -> CommandResult:
    garmin_profile = load_garmin_profile_status(settings)
    if garmin_profile["profile_error"]:
        return CommandResult(False, "Garmin upload check", str(garmin_profile["profile_error"]))
    if not garmin_profile["configured"]:
        missing = []
        if not garmin_profile["username"]:
            missing.append("Garmin email")
        if not garmin_profile["password_saved"]:
            missing.append("Garmin password")
        if not garmin_profile["product_id"]:
            missing.append("Product ID")
        if not garmin_profile["unit_id"]:
            missing.append("Unit ID")
        return CommandResult(
            False,
            "Garmin upload check",
            "The Garmin upload profile is incomplete. Missing: "
            + ", ".join(missing or ["profile details"])
            + ".",
        )

    details = [
        "Garmin FIT SDK rewrite is available.",
        f"Profile: {garmin_profile['profile']}",
        f"Garmin account: {garmin_profile['username']}",
        "Garmin password: saved",
        (
            "Device: "
            f"manufacturer {garmin_profile['manufacturer_id']}, "
            f"product {garmin_profile['product_id']}, "
            f"unit {garmin_profile['unit_id']}"
        ),
    ]
    if garmin_profile["software_version"]:
        details.append(f"Software version: {garmin_profile['software_version']}")
    details.append("The next bridge upload will use this saved profile.")
    return CommandResult(True, "Garmin upload check", "\n".join(details))


def _run(command: list[str], title: str) -> CommandResult:
    try:
        completed = subprocess.run(  # nosec B603
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(False, title, "Timed out after 30 seconds.")

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if len(output) > 4000:
        output = output[-4000:]
    rendered_command = shlex.join(command)
    prefix = f"$ {rendered_command}\n"
    return CommandResult(completed.returncode == 0, title, prefix + (output or "No output."))


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _quote_env(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() or char in "#'\"" for char in value):
        return shlex.quote(value)
    return value


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _safe_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _visible_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return [item for item in path.rglob("*") if item.is_file() and not item.name.startswith(".")]


def _string_or_empty(value: object) -> str:
    return "" if value is None else str(value)


def _device_summary(device_name: str, product_id: str, unit_id: str, software_version: str) -> str:
    details = []
    if product_id:
        details.append(f"product {product_id}")
    if unit_id:
        details.append(f"unit {unit_id}")
    if software_version:
        details.append(f"software {software_version}")
    return f"{device_name} - {' - '.join(details)}" if details else device_name


def _matched_detected_device_id(settings: Settings, garmin_profile: dict[str, object]) -> str:
    unit_id = str(garmin_profile.get("unit_id") or "")
    product_id = str(garmin_profile.get("product_id") or "")
    device = _matched_detected_device(settings, product_id, unit_id)
    return device.id if device is not None else ""


def _matched_detected_device(settings: Settings, product_id: str, unit_id: str):
    if not unit_id or not product_id:
        return None
    try:
        from app.garmin_device import garmin_device_presets, load_detected_devices
    except Exception:
        return None
    for device in load_detected_devices(settings):
        if str(device.unit_id) == unit_id and str(device.product_id) == product_id:
            return device
    for preset in garmin_device_presets():
        if str(preset.unit_id) == unit_id and str(preset.product_id) == product_id:
            return preset
    return None


def _software_version_display(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.isdigit():
        return f"{int(value) / 100:.2f}"
    return value
