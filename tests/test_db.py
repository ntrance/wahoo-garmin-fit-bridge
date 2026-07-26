from __future__ import annotations

from app.db import Database
from app.garmin_upload import GARMIN_LOGIN_CHALLENGE_MESSAGE


def test_database_initializes_wal_once_per_instance(settings):
    db = Database(settings.sqlite_path)

    assert not db._wal_set
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db._wal_set

    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db._wal_set


def test_insert_update_and_stats(settings):
    db = Database(settings.sqlite_path)
    db.init()

    activity = db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/incoming/ride.fit",
        filename="ride.fit",
        sha256="abc",
        file_size=12,
        activity_start_time="2026-06-16T10:00:00Z",
        total_distance_meters=10358.98,
    )

    updated = db.update_activity(activity["id"], status="uploaded", uploaded_at="now")

    assert updated["status"] == "uploaded"
    assert db.find_by_sha256("abc")["id"] == activity["id"]
    assert db.find_by_sha256("abc")["total_distance_meters"] == 10358.98
    assert db.find_by_start_time("2026-06-16T10:00:00Z")["id"] == activity["id"]
    assert db.stats()["uploaded"] == 1
    assert db.stats()["total"] == 1


def test_list_cleanup_candidates_only_failed_and_duplicate(settings):
    db = Database(settings.sqlite_path)
    db.init()
    db.create_activity(
        source_path="/data/incoming/uploaded.fit",
        current_path="/data/uploaded/uploaded.fit",
        filename="uploaded.fit",
        sha256="uploaded",
        file_size=12,
        activity_start_time=None,
        status="uploaded",
    )
    failed = db.create_activity(
        source_path="/data/incoming/failed.fit",
        current_path="/data/failed/failed.fit",
        filename="failed.fit",
        sha256="failed",
        file_size=12,
        activity_start_time=None,
        status="failed",
    )
    duplicate = db.create_activity(
        source_path="/data/incoming/duplicate.fit",
        current_path="/data/duplicate/duplicate.fit",
        filename="duplicate.fit",
        sha256="duplicate",
        file_size=12,
        activity_start_time=None,
        status="duplicate",
    )

    candidates = db.list_cleanup_candidates()

    assert [candidate["id"] for candidate in candidates] == [duplicate["id"], failed["id"]]


def test_repair_terminal_processing_statuses(settings):
    db = Database(settings.sqlite_path)
    db.init()
    activity = db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/processing/ride.fit",
        filename="ride.fit",
        sha256="repair",
        file_size=12,
        activity_start_time=None,
        status="processing",
    )
    db.update_activity(activity["id"], garmin_upload_status="failed", garmin_response="MFA required")

    repaired = db.repair_terminal_processing_statuses()
    updated = db.get_activity(activity["id"])

    assert repaired == 1
    assert updated["status"] == "failed"


def test_repair_raw_garmin_login_errors(settings):
    db = Database(settings.sqlite_path)
    db.init()
    activity = db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/failed/ride.fit",
        filename="ride.fit",
        sha256="raw-garmin-error",
        file_size=12,
        activity_start_time=None,
        status="failed",
        error_message="File not found after restart",
        garmin_response="GarminConnectAuthenticationError: MFA Required but no prompt_mfa mechanism supplied",
    )

    repaired = db.repair_raw_garmin_login_errors(GARMIN_LOGIN_CHALLENGE_MESSAGE)
    updated = db.get_activity(activity["id"])

    assert repaired == 1
    assert updated["error_message"] == "File not found after restart"
    assert "saved Garmin token" in updated["garmin_response"]
    assert "prompt_mfa" not in updated["garmin_response"]


def test_mark_dry_runs_already_on_garmin(settings):
    db = Database(settings.sqlite_path)
    db.init()
    activity = db.create_activity(
        source_path="/data/incoming/imported.fit",
        current_path="/data/incoming/imported.fit",
        filename="imported.fit",
        sha256="imported-sha",
        file_size=123,
        activity_start_time="2026-01-01T10:00:00Z",
        status="dry_run",
    )

    assert db.mark_dry_runs_already_on_garmin() == 1

    updated = db.get_activity(activity["id"])
    assert updated is not None
    assert updated["status"] == "already_on_garmin"
    assert updated["garmin_upload_status"] == "already_on_garmin"
    assert db.find_by_start_time("2026-01-01T10:00:00Z") == updated
