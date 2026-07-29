from __future__ import annotations

import sqlite3
from dataclasses import replace

from app.db import Database
from app.fit_metadata import FitMetadata
from app.jobs import BridgeService
from app.source_manager import SourceManager
from app.source_scheduler import SourceScheduler
from app.sources.base import (
    SourceFileMetadata,
    SourceResult,
    SourceSyncResult,
    write_source_sidecar,
)
from conftest import write_fit_like


class FakeSource:
    def __init__(self, source_type, interval, result):
        self.source_type = source_type
        self.display_name = source_type.title()
        self.poll_seconds = interval
        self.result = result
        self.calls = 0

    def is_enabled(self):
        return True

    def is_configured(self):
        return True

    def test_connection(self):
        return SourceResult(True, self.display_name, "connected")

    def sync_to_incoming(self, *, historical=False):
        self.calls += 1
        return self.result

    def supports_remote_delete(self):
        return self.source_type == "dropbox"

    def delete_remote_activity(self, external_id):
        return SourceResult(True, "delete", external_id)


def test_scheduler_reconfigure_forces_sources_due(settings):
    database = Database(settings.sqlite_path)
    first_manager = SourceManager(settings, database)
    first_bridge = BridgeService(settings, database)
    scheduler = SourceScheduler(first_manager, first_bridge)
    scheduler._next_runs["dropbox"] = 999.0

    updated_settings = replace(settings, igpsport_source_enabled=True)
    updated_manager = SourceManager(updated_settings, database)
    updated_bridge = BridgeService(updated_settings, database)
    scheduler.reconfigure(updated_manager, updated_bridge)

    assert scheduler.source_manager is updated_manager
    assert scheduler.bridge is updated_bridge
    assert scheduler._next_runs == {}


def test_source_manager_continues_after_one_source_fails(settings):
    db = Database(settings.sqlite_path)
    db.init()
    failed = FakeSource(
        "dropbox",
        60,
        SourceSyncResult(False, "Dropbox", "temporary failure"),
    )
    succeeded = FakeSource(
        "igpsport",
        900,
        SourceSyncResult(True, "iGPSPORT", "downloaded", downloaded=1),
    )
    manager = SourceManager(settings, db, sources=[failed, succeeded])

    results = manager.sync_all(manual=True)

    assert not results["dropbox"].ok
    assert results["igpsport"].ok
    assert failed.calls == succeeded.calls == 1
    assert db.get_source_state("dropbox")["consecutive_failures"] == 1
    assert db.get_source_state("igpsport")["consecutive_failures"] == 0


def test_source_backoff_increases_and_resets(settings):
    db = Database(settings.sqlite_path)
    db.init()
    source = FakeSource(
        "igpsport",
        900,
        SourceSyncResult(False, "iGPSPORT", "failure"),
    )
    manager = SourceManager(settings, db, sources=[source])

    first = manager.sync_source("igpsport")
    assert not first.ok
    first_state = db.get_source_state("igpsport")
    assert first_state["consecutive_failures"] == 1

    db.update_source_state("igpsport", backoff_until=None)
    manager.sync_source("igpsport")
    second_state = db.get_source_state("igpsport")
    assert second_state["consecutive_failures"] == 2

    db.update_source_state("igpsport", backoff_until=None)
    source.result = SourceSyncResult(True, "iGPSPORT", "success")
    manager.sync_source("igpsport")
    success_state = db.get_source_state("igpsport")
    assert success_state["consecutive_failures"] == 0
    assert success_state["backoff_until"] is None


def test_manual_sync_respects_active_source_lock(settings):
    db = Database(settings.sqlite_path)
    db.init()
    source = FakeSource(
        "igpsport",
        900,
        SourceSyncResult(True, "iGPSPORT", "success"),
    )
    manager = SourceManager(settings, db, sources=[source])
    manager._locks["igpsport"].acquire()
    try:
        result = manager.sync_source("igpsport", manual=True)
    finally:
        manager._locks["igpsport"].release()

    assert not result.ok
    assert "already running" in result.message
    assert source.calls == 0


def test_sources_keep_independent_polling_intervals(settings):
    db = Database(settings.sqlite_path)
    db.init()
    dropbox = FakeSource("dropbox", 60, SourceSyncResult(True, "Dropbox", "ok"))
    igpsport = FakeSource(
        "igpsport",
        900,
        SourceSyncResult(True, "iGPSPORT", "ok"),
    )
    manager = SourceManager(settings, db, sources=[dropbox, igpsport])

    statuses = {
        status["source_type"]: status for status in manager.statuses()
    }

    assert statuses["dropbox"]["poll_seconds"] == 60
    assert statuses["igpsport"]["poll_seconds"] == 900


def test_cross_source_same_start_time_is_marked_duplicate(
    settings,
    monkeypatch,
):
    settings = replace(settings, dry_run=True)
    settings.ensure_directories()
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db)
    service.setup()
    metadata = FitMetadata(
        sha256="dropbox-sha",
        file_size=12,
        activity_start_time="2026-07-27T10:00:00Z",
        total_distance_meters=1000,
    )
    monkeypatch.setattr("app.jobs.compute_fit_metadata", lambda path: metadata)
    first = write_fit_like(settings.incoming_dir / "dropbox.fit")
    write_source_sidecar(
        first,
        SourceFileMetadata("dropbox", "dropbox/1", "Dropbox", "ride.fit"),
    )
    assert service.scan_once()["discovered"] == 1

    second = write_fit_like(settings.incoming_dir / "igpsport.fit", b"different")
    monkeypatch.setattr(
        "app.jobs.compute_fit_metadata",
        lambda path: (
            metadata
            if path.name == "dropbox.fit"
            else FitMetadata(
                sha256="igpsport-sha",
                file_size=9,
                activity_start_time="2026-07-27T10:00:00Z",
                total_distance_meters=1000,
            )
        ),
    )
    write_source_sidecar(
        second,
        SourceFileMetadata("igpsport", "igpsport-1", "iGPSPORT Cloud", "ride.fit"),
    )
    assert service.scan_once()["discovered"] == 1

    duplicate = db.list_recent(1)[0]
    assert duplicate["status"] == "duplicate"
    assert duplicate["source_type"] == "igpsport"
    assert duplicate["duplicate_of_activity_id"] == 1


def test_cross_source_same_hash_preserves_source_reference_without_second_upload(
    settings,
    monkeypatch,
):
    settings = replace(settings, dry_run=True)
    settings.ensure_directories()
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db)
    service.setup()
    monkeypatch.setattr(
        "app.jobs.compute_fit_metadata",
        lambda path: FitMetadata("same-sha", 12, None, None),
    )
    first = write_fit_like(settings.incoming_dir / "dropbox.fit")
    write_source_sidecar(
        first,
        SourceFileMetadata("dropbox", "dropbox/1", "Dropbox"),
    )
    service.scan_once()
    second = write_fit_like(settings.incoming_dir / "igpsport.fit")
    write_source_sidecar(
        second,
        SourceFileMetadata("igpsport", "igpsport-1", "iGPSPORT Cloud"),
    )

    result = service.scan_once()

    assert result["discovered"] == 0
    assert db.stats()["total"] == 1
    assert db.is_source_item_known("igpsport", "igpsport-1")
    references = db.list_source_items_for_activity(1)
    assert {reference["source_type"] for reference in references} == {
        "dropbox",
        "igpsport",
    }


def test_database_migration_adds_source_fields_without_losing_rows(settings):
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                current_path TEXT,
                filename TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
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
                uploaded_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO activities (
                source_path, current_path, filename, sha256, file_size,
                activity_start_time, status, first_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "/data/incoming/ELEMNT-ride.fit",
                "/data/uploaded/ELEMNT-ride.fit",
                "ELEMNT-ride.fit",
                "migration-sha",
                123,
                None,
                "uploaded",
                "2026-01-01T00:00:00Z",
            ),
        )
    db = Database(settings.sqlite_path)
    db.init()
    db.init()

    migrated = db.get_activity(1)
    assert migrated is not None
    assert migrated["source_type"] == "dropbox"
    assert migrated["sha256"] == "migration-sha"
    db.record_source_item(source_type="igpsport", source_external_id="ride-1")
    assert db.is_source_item_known("igpsport", "ride-1")


def test_source_external_id_is_unique_per_source(settings):
    db = Database(settings.sqlite_path)
    db.init()
    values = {
        "source_path": "/data/incoming/ride.fit",
        "current_path": "/data/incoming/ride.fit",
        "filename": "ride.fit",
        "file_size": 12,
        "activity_start_time": None,
        "source_type": "igpsport",
        "source_external_id": "ride-1",
    }
    db.create_activity(sha256="first-sha", **values)

    try:
        db.create_activity(sha256="second-sha", **values)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Duplicate source external ID was accepted")
