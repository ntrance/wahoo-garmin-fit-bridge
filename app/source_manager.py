from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Database, utc_now
from app.settings import Settings
from app.sources.base import ActivitySource, SourceResult, SourceSyncResult
from app.sources.dropbox import DropboxSource
from app.sources.igpsport import IGPSportSource

logger = logging.getLogger(__name__)


class SourceManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        sources: list[ActivitySource] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        configured_sources = sources or [
            DropboxSource(settings, db),
            IGPSportSource(settings, db),
        ]
        self.sources = {source.source_type: source for source in configured_sources}
        self._locks = {
            source_type: threading.Lock() for source_type in self.sources
        }

    def enabled_sources(self) -> list[ActivitySource]:
        return [source for source in self.sources.values() if source.is_enabled()]

    def get(self, source_type: str) -> ActivitySource:
        try:
            return self.sources[source_type]
        except KeyError as exc:
            raise KeyError(f"Unknown activity source: {source_type}") from exc

    def statuses(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for source in self.sources.values():
            statuses.append(
                {
                    "source_type": source.source_type,
                    "display_name": source.display_name,
                    "enabled": source.is_enabled(),
                    "configured": source.is_configured(),
                    "supports_remote_delete": source.supports_remote_delete(),
                    "poll_seconds": source.poll_seconds,
                    **self.db.get_source_state(source.source_type),
                }
            )
        return statuses

    def test_connection(self, source_type: str) -> SourceResult:
        return self.get(source_type).test_connection()

    def sync_source(self, source_type: str, *, manual: bool = False) -> SourceSyncResult:
        source = self.get(source_type)
        if not source.is_enabled():
            return SourceSyncResult(
                False,
                f"{source.display_name} sync",
                f"{source.display_name} is disabled.",
            )
        if not source.is_configured():
            return SourceSyncResult(
                False,
                f"{source.display_name} sync",
                f"{source.display_name} is not configured.",
                requires_attention=True,
            )
        state = self.db.get_source_state(source_type)
        backoff_until = _parse_time(str(state.get("backoff_until") or ""))
        if backoff_until and backoff_until > datetime.now(UTC):
            return SourceSyncResult(
                False,
                f"{source.display_name} sync",
                f"{source.display_name} is paused until {backoff_until.isoformat()}.",
                retry_after_seconds=max(
                    1,
                    int((backoff_until - datetime.now(UTC)).total_seconds()),
                ),
            )
        lock = self._locks[source_type]
        if not lock.acquire(blocking=False):
            return SourceSyncResult(
                False,
                f"{source.display_name} sync",
                f"{source.display_name} sync is already running.",
            )
        try:
            self.db.update_source_state(source_type, last_poll_at=utc_now())
            result = source.sync_to_incoming()
            self._record_result(source, result, manual=manual)
            return result
        except Exception:
            logger.exception("%s source sync failed unexpectedly", source.display_name)
            result = SourceSyncResult(
                False,
                f"{source.display_name} sync",
                f"{source.display_name} sync failed unexpectedly. Check the application logs.",
            )
            self._record_result(source, result, manual=manual)
            return result
        finally:
            lock.release()

    def sync_all(self, *, manual: bool = False) -> dict[str, SourceSyncResult]:
        return {
            source.source_type: self.sync_source(source.source_type, manual=manual)
            for source in self.enabled_sources()
        }

    def delete_remote_activity(self, activity: dict[str, Any]) -> SourceResult:
        source_type = str(activity.get("source_type") or "local")
        source = self.get(source_type)
        if not source.supports_remote_delete():
            return SourceResult(
                False,
                "Delete source activity",
                f"{source.display_name} does not support remote deletion.",
            )
        external_id = str(
            activity.get("source_external_id")
            or activity.get("source_remote_path")
            or activity.get("filename")
            or ""
        )
        return source.delete_remote_activity(external_id)

    def import_igpsport_history(
        self,
        *,
        start_date: str,
        end_date: str,
        max_activities: int,
        dry_run: bool,
    ) -> SourceSyncResult:
        source = self.get("igpsport")
        if not isinstance(source, IGPSportSource):
            raise RuntimeError("The iGPSPORT source is unavailable.")
        if not source.is_enabled():
            return SourceSyncResult(
                False,
                "iGPSPORT historical import",
                "Enable the iGPSPORT source before importing history.",
            )
        if not source.is_configured():
            return SourceSyncResult(
                False,
                "iGPSPORT historical import",
                "Configure the iGPSPORT profile before importing history.",
                requires_attention=True,
            )
        lock = self._locks["igpsport"]
        if not lock.acquire(blocking=False):
            return SourceSyncResult(
                False,
                "iGPSPORT historical import",
                "iGPSPORT sync is already running.",
            )
        try:
            return source.import_history(
                start_date=start_date,
                end_date=end_date,
                max_activities=max_activities,
                dry_run=dry_run,
            )
        finally:
            lock.release()

    def _record_result(
        self,
        source: ActivitySource,
        result: SourceSyncResult,
        *,
        manual: bool,
    ) -> None:
        del manual
        now = datetime.now(UTC)
        if result.ok:
            self.db.update_source_state(
                source.source_type,
                last_success_at=utc_now(),
                last_error=None,
                consecutive_failures=0,
                backoff_until=None,
            )
            return
        state = self.db.get_source_state(source.source_type)
        failures = int(state.get("consecutive_failures") or 0) + 1
        backoff_seconds = min(900 * (2 ** (failures - 1)), 21_600)
        if result.requires_attention:
            backoff_seconds = max(backoff_seconds, 21_600)
        if result.retry_after_seconds:
            backoff_seconds = max(backoff_seconds, result.retry_after_seconds)
        self.db.update_source_state(
            source.source_type,
            last_error=result.message,
            consecutive_failures=failures,
            backoff_until=(now + timedelta(seconds=backoff_seconds))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        )


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None
