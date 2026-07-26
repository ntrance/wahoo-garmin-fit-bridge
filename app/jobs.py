from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

from app.db import Database, utc_now
from app.garmin_upload import (
    GARMIN_LOGIN_CHALLENGE_MESSAGE,
    GarminUploadResult,
    friendly_upload_error,
    run_garmin_upload,
)
from app.fit_metadata import FitMetadata, compute_fit_metadata, extract_fit_summary
from app.setup_status import delete_dropbox_source, sync_dropbox_to_incoming
from app.settings import Settings

logger = logging.getLogger(__name__)
GarminRunner = Callable[[Path, Settings], GarminUploadResult]


class BridgeService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        garmin_runner: GarminRunner = run_garmin_upload,
    ) -> None:
        self.settings = settings
        self.db = db
        self.garmin_runner = garmin_runner
        self._lock = threading.Lock()

    def setup(self) -> None:
        self.settings.ensure_directories()
        self.db.init()
        repaired = self.db.repair_raw_garmin_login_errors(GARMIN_LOGIN_CHALLENGE_MESSAGE)
        if repaired:
            logger.info("Cleaned %s stored Garmin login error row(s)", repaired)
        backfilled = self._backfill_missing_distances()
        if backfilled:
            logger.info("Backfilled distance for %s activity row(s)", backfilled)

    def scan_once(self) -> dict[str, int]:
        if not self._lock.acquire(blocking=False):
            logger.info("Scan skipped because another scan is running")
            return {"discovered": 0, "processed": 0}
        try:
            repaired = self.db.repair_terminal_processing_statuses()
            if repaired:
                logger.info("Repaired %s interrupted processing row(s)", repaired)
            discovered = self._discover_files()
            processed = self._process_pending()
            return {"discovered": discovered, "processed": processed}
        finally:
            self._lock.release()

    async def run_forever(self) -> None:
        while True:
            sync_result = await asyncio.to_thread(sync_dropbox_to_incoming, self.settings)
            if not sync_result.ok:
                logger.warning("Dropbox polling failed: %s", sync_result.output)
            await asyncio.to_thread(self.scan_once)
            await asyncio.sleep(self.settings.poll_seconds)

    def retry_now(self, activity_id: int, reset_retries: bool = False) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another scan or upload is already running")
        try:
            fields: dict[str, object] = {
                "status": "new",
                "error_message": None,
                "garmin_response": "Manual retry requested",
            }
            if reset_retries:
                fields["retry_count"] = 0
            activity = self.db.update_activity(activity_id, **fields)
            return self._process_activity(activity)
        finally:
            self._lock.release()

    def mark_ignored(self, activity_id: int) -> dict[str, object]:
        return self.db.update_activity(
            activity_id,
            status="ignored",
            error_message=None,
            garmin_response="Marked ignored from web UI",
        )

    def delete_dropbox_file(self, activity_id: int) -> dict[str, object]:
        activity = self.db.get_activity(activity_id)
        if activity is None:
            raise KeyError(activity_id)
        if activity["status"] not in {"failed", "duplicate"}:
            raise RuntimeError("Only failed or duplicate activities can be deleted from Dropbox.")

        result = delete_dropbox_source(self.settings, str(activity["filename"]))
        fields: dict[str, object] = {
            "garmin_response": result.output,
            "last_attempt_at": utc_now(),
        }
        if result.ok:
            fields.update(
                {
                    "status": "dropbox_deleted",
                    "garmin_upload_status": "dropbox_deleted",
                    "error_message": None,
                }
            )
        else:
            fields["error_message"] = result.output
        return self.db.update_activity(activity_id, **fields)

    def _backfill_missing_distances(self) -> int:
        updated = 0
        for activity in self.db.list_recent(200):
            if activity.get("total_distance_meters") is not None:
                continue
            current_path: Path | None = None
            current_path_value = str(activity.get("current_path") or "")
            if current_path_value:
                candidate = Path(current_path_value)
                if candidate.is_file():
                    current_path = candidate
            if current_path is None:
                incoming_path = self.settings.incoming_dir / str(activity.get("filename") or "")
                if incoming_path.is_file():
                    current_path = incoming_path
            if current_path is None:
                continue
            _, total_distance_meters = extract_fit_summary(current_path)
            if total_distance_meters is None:
                continue
            self.db.update_activity(
                int(activity["id"]),
                total_distance_meters=total_distance_meters,
            )
            updated += 1
        return updated

    def _discover_files(self) -> int:
        count = 0
        for fit_path in sorted(self.settings.incoming_dir.iterdir()):
            if not fit_path.is_file() or fit_path.suffix.lower() != ".fit":
                continue
            if self._discover_file(fit_path):
                count += 1
        return count

    def _discover_file(self, fit_path: Path) -> bool:
        metadata = compute_fit_metadata(fit_path)
        existing_hash = self.db.find_by_sha256(metadata.sha256)
        if existing_hash is not None:
            if self._same_known_file(existing_hash, fit_path):
                return False
            fit_path.unlink()
            logger.info(
                "Removed repeated source file already recorded as activity %s: %s",
                existing_hash["id"],
                fit_path,
            )
            return False

        duplicate_reason = self._duplicate_reason(metadata, fit_path)
        if duplicate_reason:
            moved_path = self._move_file(fit_path, self.settings.duplicate_dir)
            self.db.create_activity(
                source_path=str(fit_path),
                current_path=str(moved_path),
                filename=fit_path.name,
                sha256=metadata.sha256,
                file_size=metadata.file_size,
                activity_start_time=metadata.activity_start_time,
                total_distance_meters=metadata.total_distance_meters,
                status="duplicate",
                garmin_response=duplicate_reason,
            )
            logger.info("Duplicate detected: %s (%s)", fit_path, duplicate_reason)
            return True

        self.db.create_activity(
            source_path=str(fit_path),
            current_path=str(fit_path),
            filename=fit_path.name,
            sha256=metadata.sha256,
            file_size=metadata.file_size,
            activity_start_time=metadata.activity_start_time,
            total_distance_meters=metadata.total_distance_meters,
            status="new",
        )
        logger.info("Discovered FIT file: %s", fit_path)
        return True

    def _duplicate_reason(self, metadata: FitMetadata, fit_path: Path) -> str | None:
        if metadata.activity_start_time:
            start_match = self.db.find_by_start_time(metadata.activity_start_time)
            if start_match is not None:
                return f"Same activity start time as activity {start_match['id']}"
        filename_match = self.db.find_by_filename_size(fit_path.name, metadata.file_size)
        if filename_match is not None:
            return f"Same filename and file size as activity {filename_match['id']}"
        return None

    def _process_pending(self) -> int:
        processed = 0
        while True:
            pending = self.db.list_pending(self.settings.max_retries, limit=1)
            if not pending:
                return processed
            self._process_activity(pending[0])
            processed += 1

    def _process_activity(self, activity: dict[str, object]) -> dict[str, object]:
        activity_id = int(activity["id"])
        current_path = Path(str(activity["current_path"] or activity["source_path"]))
        if not current_path.exists():
            fallback_path = Path(str(activity["source_path"]))
            incoming_path = self.settings.incoming_dir / str(activity["filename"])
            if fallback_path.exists():
                current_path = fallback_path
            elif incoming_path.exists():
                current_path = incoming_path
            else:
                return self.db.update_activity(
                    activity_id,
                    status="failed",
                    error_message=f"File not found: {current_path}",
                    last_attempt_at=utc_now(),
                )

        if self.settings.dry_run:
            logger.info("Dry-run mode: not uploading %s", current_path)
            return self.db.update_activity(
                activity_id,
                status="dry_run",
                garmin_upload_status="dry_run",
                garmin_response="Dry run: file discovered but not uploaded",
                last_attempt_at=utc_now(),
            )

        processing_path = self._move_file(current_path, self.settings.processing_dir)
        retry_count = int(activity.get("retry_count") or 0) + 1
        self.db.update_activity(
            activity_id,
            status="processing",
            current_path=str(processing_path),
            retry_count=retry_count,
            last_attempt_at=utc_now(),
            error_message=None,
        )

        logger.info("Garmin FIT rewrite/upload started for %s", processing_path)
        result = self.garmin_runner(processing_path, self.settings)
        response = result.combined_output[-4000:] if result.combined_output else ""

        if result.duplicate:
            duplicate_path = self._move_file(processing_path, self.settings.duplicate_dir)
            logger.info("Garmin duplicate/conflict for %s", duplicate_path)
            return self.db.update_activity(
                activity_id,
                status="duplicate",
                current_path=str(duplicate_path),
                garmin_upload_status="duplicate",
                garmin_response=response,
            )

        if result.success:
            uploaded_path = self._move_file(processing_path, self.settings.uploaded_dir)
            logger.info("Upload success for %s", uploaded_path)
            return self.db.update_activity(
                activity_id,
                status="uploaded",
                current_path=str(uploaded_path),
                garmin_upload_status="uploaded",
                garmin_response=response,
                uploaded_at=utc_now(),
                error_message=None,
            )

        failed_path = self._move_file(processing_path, self.settings.failed_dir)
        error = friendly_upload_error(response, result.return_code)
        logger.error("Upload failure for %s: %s", failed_path, error)
        return self.db.update_activity(
            activity_id,
            status="failed",
            current_path=str(failed_path),
            garmin_upload_status="failed",
            garmin_response=response,
            error_message=error,
        )

    def _move_file(self, source: Path, destination_dir: Path) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = self._safe_destination(destination_dir, source.name)
        if source.resolve() == destination.resolve():
            return destination
        shutil.move(str(source), str(destination))
        return destination

    def _safe_destination(self, destination_dir: Path, filename: str) -> Path:
        candidate = destination_dir / filename
        if not candidate.exists():
            return candidate
        prefix = utc_now().replace(":", "").replace("-", "")
        return destination_dir / f"{prefix}_{filename}"

    def _same_known_file(self, activity: dict[str, object], fit_path: Path) -> bool:
        known_paths = [activity.get("source_path"), activity.get("current_path")]
        resolved = fit_path.resolve()
        for known_path in known_paths:
            if not known_path:
                continue
            try:
                if Path(str(known_path)).resolve() == resolved:
                    return True
            except OSError:
                continue
        return False
