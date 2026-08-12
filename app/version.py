from __future__ import annotations

import os


def get_app_version() -> str:
    """Return the installed application version."""
    version = os.environ.get("APP_VERSION", "").strip()
    if version and version != "dev":
        return version
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("wahoo-garmin-fit-bridge")
    except Exception:
        return "dev"
