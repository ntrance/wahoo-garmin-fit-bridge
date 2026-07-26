from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.private_files import write_private_text
from app.settings import Settings

GARMIN_RATE_LIMIT_MINUTES = 180


def garmin_guard_path(settings: Settings) -> Path:
    return settings.garmin_config_dir / "garmin-login-guard.json"


def detects_garmin_block(text: str) -> bool:
    lowered = (text or "").lower()
    rate_limited = "429" in lowered and (
        "rate limit" in lowered
        or "rate-limited" in lowered
        or "rate limited" in lowered
        or "too many requests" in lowered
    )
    cloudflare_blocked = "403" in lowered and "cloudflare" in lowered and "bot challenge" in lowered
    return rate_limited or cloudflare_blocked


def detects_garmin_rate_limit(text: str) -> bool:
    return detects_garmin_block(text)


def record_garmin_rate_limit(settings: Settings, reason: str) -> dict[str, str]:
    until = datetime.now(timezone.utc) + timedelta(minutes=GARMIN_RATE_LIMIT_MINUTES)
    data = {
        "until": until.isoformat().replace("+00:00", "Z"),
        "reason": _short_reason(reason),
    }
    path = garmin_guard_path(settings)
    write_private_text(path, json.dumps(data, indent=2))
    return data


def active_garmin_cooldown(settings: Settings) -> dict[str, str] | None:
    path = garmin_guard_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until = _parse_utc(str(data.get("until", "")))
    except Exception:
        return None
    if until <= datetime.now(timezone.utc):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return {
        "until": until.isoformat().replace("+00:00", "Z"),
        "reason": str(data.get("reason") or "Garmin login was rate-limited."),
        "message": garmin_cooldown_message(until),
    }


def clear_garmin_cooldown(settings: Settings) -> bool:
    path = garmin_guard_path(settings)
    if not path.exists():
        return False
    path.unlink()
    return True


def garmin_cooldown_message(until: datetime) -> str:
    visible_until = until.strftime("%Y-%m-%d %H:%M UTC")
    return (
        "Garmin is rate-limiting automated login attempts from this connection. "
        f"The bridge has paused Garmin login/upload attempts until {visible_until}. "
        "Do not keep retrying during this window."
    )


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _short_reason(reason: str) -> str:
    compact = " ".join((reason or "").split())
    return compact[:500]
