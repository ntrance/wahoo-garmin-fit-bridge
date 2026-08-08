from __future__ import annotations

from datetime import datetime
import os
from typing import NamedTuple
from zoneinfo import ZoneInfo


class HardwareProfile(NamedTuple):
    tier: str
    max_workers: int
    recommended_scan_interval: int


def get_hardware_profile() -> HardwareProfile:
    # Check explicit env override
    env_tier = os.getenv("HARDWARE_PROFILE", "").lower().strip()
    if env_tier == "low":
        return HardwareProfile(tier="low", max_workers=2, recommended_scan_interval=60)
    elif env_tier == "balanced":
        return HardwareProfile(tier="balanced", max_workers=4, recommended_scan_interval=30)
    elif env_tier == "high":
        return HardwareProfile(tier="high", max_workers=8, recommended_scan_interval=15)

    # Auto-detect total RAM in MB
    total_mem_mb = 0.0
    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_mem_mb = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass

    cores = os.cpu_count() or 1

    if total_mem_mb > 0 and total_mem_mb <= 1200:
        # Low spec: Raspberry Pi 3 / Pi Zero 2W (1GB RAM or less)
        return HardwareProfile(tier="low", max_workers=2, recommended_scan_interval=60)
    elif total_mem_mb <= 4096:
        # Mid spec: Raspberry Pi 4 (2GB-4GB RAM)
        return HardwareProfile(tier="balanced", max_workers=4, recommended_scan_interval=30)
    else:
        # High spec: Raspberry Pi 5 / Server / PC (> 4GB RAM)
        return HardwareProfile(tier="high", max_workers=min(8, max(4, cores)), recommended_scan_interval=15)


TIMEZONE_ALIASES: dict[str, str] = {
    "london": "Europe/London",
    "uk": "Europe/London",
    "gb": "Europe/London",
    "bst": "Europe/London",
    "gmt": "Europe/London",
    "etc/lon": "Europe/London",
    "etc/london": "Europe/London",
}


def normalize_timezone_name(tz_input: str) -> str:
    cleaned = tz_input.strip().strip("'\"")
    if not cleaned:
        return "Europe/London"

    alias_match = TIMEZONE_ALIASES.get(cleaned.lower())
    if alias_match:
        return alias_match

    try:
        ZoneInfo(cleaned)
        return cleaned
    except Exception:
        pass

    for name in (
        "Europe/London",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "Europe/Paris",
        "Europe/Berlin",
        "Australia/Sydney",
        "UTC",
    ):
        if name.lower() == cleaned.lower():
            return name

    return "Europe/London"


def detect_system_timezone() -> str:
    env_tz = (os.getenv("TZ") or os.getenv("TIMEZONE") or "").strip()
    if env_tz:
        norm = normalize_timezone_name(env_tz)
        if norm:
            return norm

    if os.path.exists("/etc/timezone"):
        try:
            with open("/etc/timezone") as f:
                tz = f.read().strip()
                if tz and tz not in {"UTC", "Etc/UTC"}:
                    return normalize_timezone_name(tz)
        except Exception:
            pass

    if os.path.islink("/etc/localtime"):
        try:
            target = os.readlink("/etc/localtime")
            if "zoneinfo/" in target:
                tz = target.split("zoneinfo/")[-1].strip()
                if tz and tz not in {"UTC", "Etc/UTC"}:
                    return normalize_timezone_name(tz)
        except Exception:
            pass

    try:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz:
            name = str(local_tz)
            if name not in {"UTC", "Etc/UTC"}:
                return normalize_timezone_name(name)
    except Exception:
        pass

    return "Europe/London"


def get_formatted_local_time(tz_name: str) -> str:
    norm_tz = normalize_timezone_name(tz_name)
    try:
        tz = ZoneInfo(norm_tz)
        now = datetime.now(tz)
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S UTC")
