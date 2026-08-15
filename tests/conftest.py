from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="test-bridge",
        poll_seconds=1,
        dropbox_source_enabled=True,
        igpsport_source_enabled=False,
        coros_source_enabled=False,
        dropbox_poll_seconds=1,
        igpsport_poll_seconds=300,
        coros_poll_seconds=300,
        igpsport_min_poll_seconds=300,
        coros_min_poll_seconds=60,
        igpsport_max_pages_per_poll=3,
        coros_max_pages_per_poll=3,
        igpsport_base_url="https://igpsport.example.test/service",
        coros_base_url="https://teamapi.coros.com",
        igpsport_config_dir=tmp_path / "appdata" / "igpsport",
        coros_config_dir=tmp_path / "appdata" / "coros",
        igpsport_import_mode="new_only",
        coros_import_mode="new_only",
        max_retries=3,
        log_level="INFO",
        web_auth_enabled=False,
        web_username="admin",
        web_password="password",
        web_password_hash="",
        session_secret_key="test-session-secret",
        session_cookie_name="test_session",
        session_cookie_secure=False,
        session_max_age_seconds=3600,
        login_rate_limit_attempts=5,
        login_rate_limit_window_seconds=300,
        dry_run=False,
        rclone_remote="dropbox",
        dropbox_wahoo_path="Apps/WahooFitness",
        rclone_config_path=tmp_path / "appdata" / "rclone" / "rclone.conf",
        incoming_dir=tmp_path / "data" / "incoming",
        processing_dir=tmp_path / "data" / "processing",
        uploaded_dir=tmp_path / "data" / "uploaded",
        duplicate_dir=tmp_path / "data" / "duplicate",
        failed_dir=tmp_path / "data" / "failed",
        archive_dir=tmp_path / "data" / "archive",
        sqlite_path=tmp_path / "appdata" / "bridge.sqlite",
        log_dir=tmp_path / "appdata" / "logs",
        runtime_config_path=tmp_path / "appdata" / "runtime.env",
        real_fit_dir=tmp_path / "real_fit",
        real_fit_upload_dir=tmp_path / "appdata" / "real_fit",
        max_real_fit_upload_bytes=33_554_432,
        detected_devices_path=tmp_path / "appdata" / "detected_devices.json",
        garmin_config_dir=tmp_path / "appdata" / "garmin",
        garmin_profile_name="wahoo",
        garmin_device_name="Fenix 6X Pro",
        garmin_unit_id="12345",
        previews_dir=tmp_path / "data" / "previews",
    )


def write_fit_like(path: Path, content: bytes = b"fake-fit-data.FIT") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
