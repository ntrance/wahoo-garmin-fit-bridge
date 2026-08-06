from __future__ import annotations

import os
import platform
from typing import NamedTuple


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
