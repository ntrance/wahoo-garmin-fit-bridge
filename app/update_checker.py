from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

try:
    from packaging.version import Version
except ImportError:
    Version = None  # type: ignore

logger = logging.getLogger(__name__)

GITHUB_REPO = "ntrance/wahoo-garmin-fit-bridge"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
RELEASE_URL_TEMPLATE = "https://github.com/ntrance/wahoo-garmin-fit-bridge/releases/tag/v{version}"
CACHE_TTL_SECONDS = 43200
REQUEST_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class UpdateStatus:
    latest_version: str | None
    update_available: bool
    release_url: str | None
    checked_at: float
    error: str | None


_cached_status: UpdateStatus | None = None


def _parse_version(tag: str) -> tuple[int, ...] | None:
    tag = tag.strip().lstrip("v").lstrip("V")
    if Version is not None:
        try:
            return Version(tag).release
        except Exception:
            pass

    try:
        return tuple(int(x) for x in tag.split("."))
    except Exception:
        return None


def _compare_versions(current: str, latest: str) -> bool:
    try:
        current_parsed = _parse_version(current)
        latest_parsed = _parse_version(latest)
        if current_parsed is None or latest_parsed is None:
            return False
        return latest_parsed > current_parsed
    except Exception:
        return False


def check_for_update(current_version: str) -> UpdateStatus:
    global _cached_status

    if _cached_status is not None:
        if time.time() - _cached_status.checked_at < CACHE_TTL_SECONDS:
            logger.debug("Returning cached update status")
            return _cached_status

    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"wahoo-garmin-fit-bridge/{current_version}",
        }
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(GITHUB_API_URL, headers=headers)
            response.raise_for_status()

        releases = response.json()

        latest_tag = None
        for release in releases:
            if not release.get("draft") and not release.get("prerelease"):
                latest_tag = release.get("tag_name")
                break

        if not latest_tag:
            status = UpdateStatus(
                latest_version=None,
                update_available=False,
                release_url=None,
                checked_at=time.time(),
                error="No stable releases found",
            )
        else:
            latest_version_str = latest_tag.lstrip("v").lstrip("V")
            update_available = _compare_versions(current_version, latest_version_str)

            release_url = RELEASE_URL_TEMPLATE.format(version=latest_version_str)

            status = UpdateStatus(
                latest_version=latest_version_str,
                update_available=update_available,
                release_url=release_url,
                checked_at=time.time(),
                error=None,
            )
            logger.debug("Successfully checked for updates")

        _cached_status = status
        return status

    except Exception as e:
        error_msg = f"Failed to check for updates: {e}"
        logger.warning(error_msg)

        if _cached_status is not None:
            return _cached_status

        status = UpdateStatus(
            latest_version=None,
            update_available=False,
            release_url=None,
            checked_at=time.time(),
            error=error_msg,
        )
        _cached_status = status
        return status


def clear_cache() -> None:
    global _cached_status
    _cached_status = None
