from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

UPDATABLE_ACTIVITY_FIELDS = frozenset(
    {
        "source_path",
        "current_path",
        "filename",
        "sha256",
        "file_size",
        "activity_start_time",
        "total_distance_meters",
        "status",
        "garmin_upload_status",
        "garmin_response",
        "error_message",
        "retry_count",
        "first_seen_at",
        "last_attempt_at",
        "uploaded_at",
    }
)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._wal_set = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._wal_set:
            conn.execute("PRAGMA journal_mode = WAL")
            self._wal_set = True
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    current_path TEXT,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    activity_start_time TEXT,
                    total_distance_meters REAL,
                    status TEXT NOT NULL,
                    garmin_upload_status TEXT,
                    garmin_response TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    uploaded_at TEXT,
                    UNIQUE(sha256)
                );

                CREATE INDEX IF NOT EXISTS idx_activities_status ON activities(status);
                CREATE INDEX IF NOT EXISTS idx_activities_start_time ON activities(activity_start_time);
                CREATE INDEX IF NOT EXISTS idx_activities_sha256 ON activities(sha256);
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(activities)").fetchall()
            }
            if "total_distance_meters" not in columns:
                conn.execute("ALTER TABLE activities ADD COLUMN total_distance_meters REAL")

    def create_activity(
        self,
        *,
        source_path: str,
        current_path: str,
        filename: str,
        sha256: str,
        file_size: int,
        activity_start_time: str | None,
        total_distance_meters: float | None = None,
        status: str = "new",
        garmin_response: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO activities (
                    source_path, current_path, filename, sha256, file_size,
                    activity_start_time, total_distance_meters, status,
                    garmin_response, error_message, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_path,
                    current_path,
                    filename,
                    sha256,
                    file_size,
                    activity_start_time,
                    total_distance_meters,
                    status,
                    garmin_response,
                    error_message,
                    now,
                ),
            )
            activity_id = cursor.lastrowid
        activity = self.get_activity(activity_id)
        if activity is None:
            raise RuntimeError("Activity insert succeeded but row was not found")
        return activity

    def update_activity(self, activity_id: int, **fields: Any) -> dict[str, Any]:
        if not fields:
            activity = self.get_activity(activity_id)
            if activity is None:
                raise KeyError(activity_id)
            return activity
        invalid_fields = fields.keys() - UPDATABLE_ACTIVITY_FIELDS
        if invalid_fields:
            raise ValueError(f"Unsupported activity fields: {', '.join(sorted(invalid_fields))}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(activity_id)
        with self.connect() as conn:
            # Identifiers are restricted to UPDATABLE_ACTIVITY_FIELDS.
            conn.execute(
                f"UPDATE activities SET {assignments} WHERE id = ?",  # nosec B608
                values,
            )
        activity = self.get_activity(activity_id)
        if activity is None:
            raise KeyError(activity_id)
        return activity

    def get_activity(self, activity_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        return dict(row) if row else None

    def find_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM activities WHERE sha256 = ?", (sha256,)).fetchone()
        return dict(row) if row else None

    def find_by_start_time(self, start_time: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM activities
                WHERE activity_start_time = ?
                  AND status IN (
                      'new',
                      'processing',
                      'uploaded',
                      'already_on_garmin',
                      'duplicate',
                      'dry_run'
                  )
                ORDER BY id ASC
                LIMIT 1
                """,
                (start_time,),
            ).fetchone()
        return dict(row) if row else None

    def find_by_filename_size(self, filename: str, file_size: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM activities
                WHERE filename = ? AND file_size = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (filename, file_size),
            ).fetchone()
        return dict(row) if row else None

    def list_file_fingerprints(self) -> set[tuple[str, int]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT filename, file_size
                FROM activities
                WHERE filename IS NOT NULL AND file_size IS NOT NULL
                """
            ).fetchall()
        return {(str(row["filename"]), int(row["file_size"])) for row in rows}

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activities ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_cleanup_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM activities
                WHERE status IN ('failed', 'duplicate')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_dry_runs_already_on_garmin(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE activities
                SET status = 'already_on_garmin',
                    garmin_upload_status = 'already_on_garmin',
                    garmin_response = 'Imported history confirmed as already present in Garmin Connect',
                    error_message = NULL
                WHERE status = 'dry_run'
                """
            )
        return cursor.rowcount

    def list_pending(self, max_retries: int, limit: int = 1) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM activities
                WHERE status IN ('new', 'failed') AND retry_count < ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (max_retries, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def repair_terminal_processing_statuses(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE activities
                SET status = garmin_upload_status
                WHERE status = 'processing'
                  AND garmin_upload_status IN ('uploaded', 'failed', 'duplicate', 'dry_run')
                """
            )
            return cursor.rowcount

    def repair_raw_garmin_login_errors(self, replacement: str) -> int:
        markers = (
            "%MFA Required%",
            "%prompt_mfa%",
            "%GarminConnectAuthenticationError%",
            "%Fit-File-Faker has no saved Garmin token%",
        )
        with self.connect() as conn:
            repaired = 0
            for marker in markers:
                cursor = conn.execute(
                    """
                    UPDATE activities
                    SET
                        error_message = CASE
                            WHEN error_message LIKE ? THEN ?
                            ELSE error_message
                        END,
                        garmin_response = CASE
                            WHEN garmin_response LIKE ? THEN ?
                            ELSE garmin_response
                        END
                    WHERE status = 'failed'
                      AND (
                        error_message LIKE ?
                        OR garmin_response LIKE ?
                      )
                    """,
                    (marker, replacement, marker, replacement, marker, marker),
                )
                repaired += cursor.rowcount
            return repaired

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM activities GROUP BY status").fetchall()
            total = conn.execute("SELECT COUNT(*) AS count FROM activities").fetchone()["count"]
        stats = {row["status"]: row["count"] for row in rows}
        stats["total"] = total
        return stats
