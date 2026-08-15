from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.garmin_profile import GarminProfile, garmin_token_dir, load_garmin_profile
from app.garmin_guard import (
    active_garmin_cooldown,
    detects_garmin_rate_limit,
    record_garmin_rate_limit,
)
from app.settings import Settings
from app.redaction import redact_sensitive_text


@dataclass(frozen=True)
class GarminUploadResult:
    success: bool
    stdout: str
    stderr: str
    return_code: int
    duplicate: bool = False
    timed_out: bool = False

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


GARMIN_LOGIN_CHALLENGE_MESSAGE = (
    "Garmin did not accept a password-only login from this container and there is no saved Garmin token "
    "to reuse. Create the Garmin session from Config, then reprocess this activity."
)


def run_garmin_upload(
    file_path: Path,
    settings: Settings,
    timeout_seconds: int = 180,
) -> GarminUploadResult:
    del timeout_seconds
    cooldown = active_garmin_cooldown(settings)
    if cooldown is not None:
        return GarminUploadResult(
            success=False,
            stdout="",
            stderr=str(cooldown["message"]),
            return_code=75,
        )

    try:
        profile = _load_profile(settings)
        device_name = _target_device_name(settings, profile)
        rewritten, rewrite_note, record_count = _rewrite_wahoo_fit(
            file_path, profile, device_name
        )
        if not rewritten:
            return GarminUploadResult(False, "", rewrite_note, 1)
        if record_count <= 0:
            return GarminUploadResult(
                success=False,
                stdout="",
                stderr="Rewritten FIT contains no activity records; upload was blocked.",
                return_code=1,
            )

        if settings.dry_run:
            stdout_parts = [
                f"Dry run: rewrote FIT as {device_name}; Garmin upload skipped.",
            ]
        else:
            client, token_dir = _login_garmin_with_profile(settings, profile)
            client.upload_activity(str(file_path))
            stdout_parts = [
                f"Rewrote Wahoo FIT and uploaded to Garmin Connect as {device_name}.",
                f"Tokens: {token_dir}",
            ]
        stdout_parts.append(rewrite_note)
        return GarminUploadResult(
            success=True,
            stdout="\n".join(stdout_parts),
            stderr="",
            return_code=0,
        )
    except Exception as exc:
        output = redact_output(str(exc), settings)
        if detects_garmin_rate_limit(output):
            record_garmin_rate_limit(settings, output)
        return GarminUploadResult(
            success=False,
            stdout="",
            stderr=friendly_upload_error(output, 1),
            return_code=1,
            duplicate=looks_like_duplicate(output),
        )


@dataclass
class GarminMFAPending:
    client: Any
    username: str
    token_dir: Path
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


def start_garmin_session_login(
    settings: Settings,
    username: str,
    password: str,
) -> tuple[bool, str, GarminMFAPending | None]:
    cooldown = active_garmin_cooldown(settings)
    if cooldown is not None:
        return False, str(cooldown["message"]), None

    if not username.strip() or not password:
        return False, "Garmin username and password are required.", None

    token_dir = garmin_token_dir(settings)
    token_dir.mkdir(parents=True, exist_ok=True)

    try:
        from garminconnect import Garmin

        client = Garmin(
            email=username.strip(),
            password=password,
            return_on_mfa=True,
        )
        mfa_status, _ = client.login(tokenstore=str(token_dir))
        if mfa_status:
            return (
                True,
                "MFA_REQUIRED",
                GarminMFAPending(client=client, username=username.strip(), token_dir=token_dir),
            )
        return (
            True,
            f"Garmin session created and saved to {token_dir}. Uploads can now reuse this token.",
            None,
        )
    except Exception as exc:
        output = redact_output(str(exc), settings)
        if detects_garmin_rate_limit(output):
            record_garmin_rate_limit(settings, output)
        return False, friendly_upload_error(output, 1), None


def complete_garmin_mfa_session(
    pending: GarminMFAPending,
    mfa_code: str,
    settings: Settings,
) -> tuple[bool, str]:
    if not mfa_code or not mfa_code.strip():
        return False, "Verification code cannot be empty."

    try:
        pending.client.client._complete_mfa(mfa_code.strip())
        pending.client._load_profile_and_settings()
        pending.token_dir.mkdir(parents=True, exist_ok=True)
        pending.client.client.dump(str(pending.token_dir))
        return (
            True,
            f"Garmin two-factor authentication successful! Session tokens saved to {pending.token_dir}.",
        )
    except Exception as exc:
        output = redact_output(str(exc), settings)
        if detects_garmin_rate_limit(output):
            record_garmin_rate_limit(settings, output)
        return False, f"Garmin MFA verification failed: {friendly_upload_error(output, 1)}"


def create_garmin_session(settings: Settings) -> GarminUploadResult:
    cooldown = active_garmin_cooldown(settings)
    if cooldown is not None:
        return GarminUploadResult(False, "", str(cooldown["message"]), 75)
    try:
        profile = _load_profile(settings)
        ok, message, pending = start_garmin_session_login(
            settings,
            getattr(profile, "garmin_username", ""),
            getattr(profile, "garmin_password", ""),
        )
        if pending is not None:
            return GarminUploadResult(
                True,
                "Garmin requested a two-factor verification code. Enter the code sent to your email.",
                "",
                0,
            )
        return GarminUploadResult(
            ok,
            message if ok else "",
            "" if ok else message,
            0 if ok else 1,
        )
    except Exception as exc:
        output = redact_output(str(exc), settings)
        if detects_garmin_rate_limit(output):
            record_garmin_rate_limit(settings, output)
        return GarminUploadResult(False, "", friendly_upload_error(output, 1), 1)


def _load_profile(settings: Settings) -> GarminProfile:
    profile = load_garmin_profile(settings)
    if profile is None:
        raise RuntimeError(
            "Garmin upload profile was not found. Save it from Config > Garmin Upload."
        )
    return profile


def _login_garmin_with_profile(settings: Settings, profile: GarminProfile):
    from garminconnect import Garmin

    token_dir = garmin_token_dir(settings)
    token_dir.mkdir(parents=True, exist_ok=True)
    client = Garmin(
        email=getattr(profile, "garmin_username", ""),
        password=getattr(profile, "garmin_password", ""),
        prompt_mfa=_mfa_not_available,
    )
    client.login(tokenstore=str(token_dir))
    return client, token_dir


def _target_device_name(settings: Settings, profile) -> str:
    try:
        from app.garmin_device import garmin_product_display_name

        product_id = int(getattr(profile, "device", 0) or 0)
        if product_id:
            return garmin_product_display_name("", product_id)
    except Exception:  # nosec B110
        pass
    name = settings.garmin_device_name.strip()
    return name if name.lower().startswith("garmin") else f"Garmin {name}"


def _rewrite_wahoo_fit(
    file_path: Path, profile: GarminProfile, device_name: str
) -> tuple[bool, str, int]:
    try:
        from garmin_fit_sdk import Decoder, Encoder, Stream
    except Exception:
        return False, "Garmin FIT SDK is unavailable.", 0

    try:
        manufacturer = int(getattr(profile, "manufacturer", 1) or 1)
        product_id = int(getattr(profile, "device", 0) or 0)
        unit_id = int(getattr(profile, "serial_number", 0) or 0)
        try:
            software_version_raw = int(getattr(profile, "software_version", 0) or 0)
        except (TypeError, ValueError):
            software_version_raw = None
        software_version = _device_info_software_version(
            getattr(profile, "software_version", None)
        )
        if not product_id:
            return False, "Garmin upload profile has no target product ID.", 0

        with file_path.open("rb") as handle:
            integrity_decoder = Decoder(Stream(handle, file_path.stat().st_size))
            if not integrity_decoder.check_integrity():
                return (
                    False,
                    "Source FIT failed Garmin's integrity check. "
                    "The Dropbox FIT appears incomplete/corrupt, so the bridge will not upload a partial activity. "
                    "Export/share the activity again from Wahoo or Strava and retry with the complete FIT.",
                    0,
                )

        ordered_messages = []
        file_timestamp = None
        with file_path.open("rb") as handle:
            decoder = Decoder(Stream(handle, file_path.stat().st_size))

            def on_message(message_number, message) -> None:
                nonlocal file_timestamp
                if file_timestamp is None:
                    file_timestamp = (
                        message.get("time_created")
                        if message_number == 0
                        else message.get("timestamp")
                    )
                ordered_messages.append((message_number, message))

            _, errors = decoder.read(merge_heart_rates=False, mesg_listener=on_message)

        if not ordered_messages:
            return False, "Garmin FIT SDK did not find any standard FIT messages.", 0

        record_count = sum(
            1 for message_number, _ in ordered_messages if message_number == 20
        )

        has_target = any(
            message_number == 23 and _is_sdk_target_device_info(message, product_id)
            for message_number, message in ordered_messages
        )
        summary_messages = _sdk_summary_messages(ordered_messages)
        has_session_summary = any(
            message_number in {18, 19, 34} for message_number, _ in ordered_messages
        )
        insert_after = 0
        for index, (message_number, _) in enumerate(ordered_messages):
            if message_number == 0:
                insert_after = index

        is_virtual = any(
            (message_number == 0 and message.get("manufacturer") in {206, 294, 300})
            or (message_number in {18, 12} and message.get("sub_sport") in {27, "virtual_activity"})
            for message_number, message in ordered_messages
        )

        encoder = Encoder()
        changed = 0
        written = 0
        skipped = 0
        inserted = False
        for index, (message_number, message) in enumerate(ordered_messages):
            if message_number == 49:
                continue
            clean_message = {
                key: value for key, value in message.items() if key != "developer_fields"
            }
            if is_virtual and message_number in {18, 12}:
                clean_message["sub_sport"] = "virtual_activity"
                changed += 1
            if message_number == 0:
                clean_message["type"] = clean_message.get("type") or "activity"
                clean_message["manufacturer"] = manufacturer
                clean_message["product"] = product_id
                clean_message["garmin_product"] = product_id
                if unit_id:
                    clean_message["serial_number"] = unit_id
                changed += 1
            if message_number == 23 and _is_sdk_target_device_info(clean_message, product_id):
                _set_sdk_device_identity(
                    clean_message,
                    manufacturer,
                    product_id,
                    unit_id,
                    software_version,
                )
                changed += 1
            try:
                encoder.on_mesg(message_number, clean_message)
                written += 1
            except ValueError as exc:
                if "could not be found in the Profile" not in str(exc):
                    raise
                skipped += 1
            if not inserted and index == insert_after:
                if software_version_raw is not None:
                    encoder.on_mesg(49, {"software_version": software_version_raw})
                    written += 1
                if not has_target:
                    encoder.on_mesg(
                        23,
                        _sdk_device_identity_message(
                            file_timestamp,
                            manufacturer,
                            product_id,
                            unit_id,
                            software_version,
                        ),
                    )
                    written += 1
                    changed += 1
                inserted = True

        if not has_session_summary and summary_messages:
            for message_number, summary_message in summary_messages:
                encoder.on_mesg(message_number, summary_message)
                written += 1
            changed += len(summary_messages)

        if written == 0:
            return False, "Garmin FIT SDK did not write any standard FIT messages.", 0

        rewritten_path = file_path.with_name(f".{file_path.name}.sdk")
        rewritten_path.write_bytes(encoder.close())
        rewritten_path.replace(file_path)
        note = (
            f"Garmin FIT SDK rewrote the Wahoo activity as {device_name} "
            f"({written} standard messages kept"
        )
        if skipped:
            note = f"{note}, {skipped} Wahoo custom messages skipped"
        if not has_session_summary and summary_messages:
            note = f"{note}, rebuilt missing activity summary"
        if errors:
            note = f"{note}, {len(errors)} decoder warning(s)"
        return True, f"{note}).", record_count
    except Exception as exc:
        return False, f"Garmin FIT SDK rewrite failed: {exc}", 0


def _sdk_summary_messages(ordered_messages: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
    records = [message for message_number, message in ordered_messages if message_number == 20]
    if not records:
        return []
    start_time = next((record.get("timestamp") for record in records if record.get("timestamp")), None)
    end_time = next((record.get("timestamp") for record in reversed(records) if record.get("timestamp")), None)
    distance = next(
        (record.get("distance") for record in reversed(records) if record.get("distance") is not None),
        None,
    )
    if start_time is None or end_time is None or distance is None:
        return []
    try:
        elapsed_seconds = max((end_time - start_time).total_seconds(), 0)
    except Exception:
        return []
    avg_speed = float(distance) / elapsed_seconds if elapsed_seconds else 0
    speeds = [
        float(speed)
        for record in records
        for speed in (record.get("enhanced_speed"), record.get("speed"))
        if speed is not None
    ]
    max_speed = max(speeds) if speeds else avg_speed

    lap = {
        "timestamp": end_time,
        "event": "lap",
        "event_type": "stop",
        "start_time": start_time,
        "total_elapsed_time": elapsed_seconds,
        "total_timer_time": elapsed_seconds,
        "total_distance": float(distance),
        "avg_speed": avg_speed,
        "enhanced_avg_speed": avg_speed,
        "max_speed": max_speed,
        "enhanced_max_speed": max_speed,
    }
    session = lap | {
        "event": "session",
        "sport": "cycling",
        "sub_sport": "generic",
        "num_laps": 1,
    }
    activity = {
        "timestamp": end_time,
        "event": "activity",
        "event_type": "stop",
        "type": "manual",
        "num_sessions": 1,
        "total_timer_time": elapsed_seconds,
    }
    return [(19, lap), (18, session), (34, activity)]


def _is_sdk_target_device_info(message: dict, product_id: int) -> bool:
    device_index = str(message.get("device_index", "")).lower()
    if device_index in {"creator", "0"}:
        return True
    product = message.get("product")
    garmin_product = message.get("garmin_product")
    return product == product_id or garmin_product == product_id or str(product) == str(product_id)


def _sdk_device_identity_message(
    timestamp,
    manufacturer: int,
    product_id: int,
    unit_id: int,
    software_version: float | int | None,
) -> dict:
    message = {
        "device_index": "creator",
        "manufacturer": manufacturer,
        "product": product_id,
        "garmin_product": product_id,
    }
    if timestamp is not None:
        message["timestamp"] = timestamp
    if unit_id:
        message["serial_number"] = unit_id
    if software_version is not None:
        message["software_version"] = software_version
    return message


def _set_sdk_device_identity(
    message: dict,
    manufacturer: int,
    product_id: int,
    unit_id: int,
    software_version: float | int | None,
) -> None:
    message["manufacturer"] = manufacturer
    message["product"] = product_id
    message["garmin_product"] = product_id
    if unit_id:
        message["serial_number"] = unit_id
    if software_version is not None:
        message["software_version"] = software_version


def _device_info_software_version(value) -> float | int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    return number / 100 if number > 100 else number


def _mfa_not_available() -> str:
    raise RuntimeError(
        "Garmin asked this container for a one-time code. Open Config and create the Garmin session there, "
        "then retry the upload."
    )


def looks_like_duplicate(output: str) -> bool:
    lowered = output.lower()
    markers = (
        "already exists",
        "duplicate",
        "http conflict",
        "409",
        "activity already exists",
    )
    return any(marker in lowered for marker in markers)


def has_garmin_login_challenge(output: str) -> bool:
    return "mfa required but no prompt_mfa mechanism supplied" in output.lower()


def friendly_upload_error(output: str, return_code: int) -> str:
    if detects_garmin_rate_limit(output):
        return (
            "Garmin is blocking or rate-limiting automated login attempts from this connection. "
            "The bridge has paused Garmin upload attempts. Wait or change network before trying again."
        )
    if has_garmin_login_challenge(output):
        return GARMIN_LOGIN_CHALLENGE_MESSAGE
    return output[-4000:] if output else f"Garmin upload failed with exit code {return_code}"


def redact_output(text: str, settings: Settings) -> str:
    return redact_sensitive_text(text, (settings.web_password, settings.garmin_unit_id))
