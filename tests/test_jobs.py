from __future__ import annotations

from pathlib import Path

from app.db import Database
from app.garmin_upload import GarminUploadResult
from app.jobs import BridgeService
from conftest import write_fit_like


def success_runner(path: Path, _settings) -> GarminUploadResult:
    assert path.exists()
    return GarminUploadResult(True, "Successfully uploaded", "", 0)


def duplicate_runner(path: Path, _settings) -> GarminUploadResult:
    assert path.exists()
    return GarminUploadResult(False, "HTTP conflict activity already exists", "", 1, duplicate=True)


def failed_runner(path: Path, _settings) -> GarminUploadResult:
    assert path.exists()
    return GarminUploadResult(False, "", "auth failed", 1)


def garmin_login_challenge_runner(path: Path, _settings) -> GarminUploadResult:
    assert path.exists()
    return GarminUploadResult(
        False,
        "",
        "GarminConnectAuthenticationError: MFA Required but no prompt_mfa mechanism supplied",
        1,
    )


def test_scan_uploads_new_file(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, success_runner)
    service.setup()
    write_fit_like(settings.incoming_dir / "ride.fit")

    result = service.scan_once()
    recent = db.list_recent()

    assert result["discovered"] == 1
    assert result["processed"] == 1
    assert recent[0]["status"] == "uploaded"
    assert (settings.uploaded_dir / "ride.fit").exists()


def test_duplicate_hash_file_is_removed_without_second_row(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, success_runner)
    service.setup()
    write_fit_like(settings.incoming_dir / "ride.fit", b"same")
    service.scan_once()
    write_fit_like(settings.incoming_dir / "copy.fit", b"same")

    service.scan_once()

    assert len(db.list_recent()) == 1
    assert not (settings.incoming_dir / "copy.fit").exists()
    assert not (settings.duplicate_dir / "copy.fit").exists()


def test_garmin_duplicate_moves_file(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, duplicate_runner)
    service.setup()
    write_fit_like(settings.incoming_dir / "ride.fit")

    service.scan_once()
    activity = db.list_recent()[0]

    assert activity["status"] == "duplicate"
    assert (settings.duplicate_dir / "ride.fit").exists()


def test_failed_job_can_retry(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, failed_runner)
    service.setup()
    write_fit_like(settings.incoming_dir / "ride.fit")
    service.scan_once()
    failed = db.list_recent()[0]

    retry_service = BridgeService(settings, db, success_runner)
    retry_service.setup()
    retry_service.retry_now(failed["id"], reset_retries=True)
    retried = db.get_activity(failed["id"])

    assert retried["status"] == "uploaded"
    assert retried["retry_count"] == 1


def test_garmin_login_challenge_error_is_readable(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, garmin_login_challenge_runner)
    service.setup()
    write_fit_like(settings.incoming_dir / "ride.fit")

    service.scan_once()
    failed = db.list_recent()[0]

    assert failed["status"] == "failed"
    assert "saved Garmin token" in failed["error_message"]
    assert "prompt_mfa" not in failed["error_message"]
    assert "MFA Required" in failed["garmin_response"]


def test_retry_uses_restored_incoming_file_when_current_path_missing(settings):
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db, success_runner)
    service.setup()
    failed = db.create_activity(
        source_path=str(settings.incoming_dir / "ride.fit"),
        current_path=str(settings.failed_dir / "ride.fit"),
        filename="ride.fit",
        sha256="restored",
        file_size=12,
        activity_start_time=None,
        status="failed",
    )
    db.update_activity(failed["id"], garmin_upload_status="failed")
    write_fit_like(settings.incoming_dir / "ride.fit")

    service.retry_now(failed["id"], reset_retries=True)
    retried = db.get_activity(failed["id"])

    assert retried["status"] == "uploaded"
    assert retried["retry_count"] == 1


def test_dry_run_does_not_move_or_upload(settings):
    dry_settings = settings.__class__(**{**settings.__dict__, "dry_run": True})
    db = Database(dry_settings.sqlite_path)
    service = BridgeService(dry_settings, db, success_runner)
    service.setup()
    write_fit_like(dry_settings.incoming_dir / "ride.fit")

    service.scan_once()
    activity = db.list_recent()[0]

    assert activity["status"] == "dry_run"
    assert (dry_settings.incoming_dir / "ride.fit").exists()
