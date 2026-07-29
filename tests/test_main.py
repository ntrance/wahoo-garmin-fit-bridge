from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db import Database
from app.garmin_guard import active_garmin_cooldown, record_garmin_rate_limit
from app.main import create_app
from app.setup_status import (
    CommandResult,
    delete_dropbox_source,
    sync_dropbox_to_incoming,
    test_garmin_upload as run_garmin_upload_test,
)
from app.sources.base import SourceResult, SourceSyncResult
from app.sources.igpsport import IGPSportStore


def test_health(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_without_auth_when_disabled(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Activities" in response.text
    assert "Last rclone copy" not in response.text


def test_session_auth_when_enabled(settings):
    authed_settings = settings.__class__(**{**settings.__dict__, "web_auth_enabled": True})
    app = create_app(authed_settings, start_background=False)
    with TestClient(app) as client:
        unauthorized = client.get("/", follow_redirects=False)
        login = client.post(
            "/login",
            data={"username": "admin", "password": "password", "next": "/"},
            follow_redirects=False,
        )
        authorized = client.get("/")

    assert unauthorized.status_code == 303
    assert unauthorized.headers["location"].startswith("/login")
    assert login.status_code == 303
    assert authorized.status_code == 200


def test_login_rejects_unsafe_redirects(settings):
    authed_settings = settings.__class__(**{**settings.__dict__, "web_auth_enabled": True})
    app = create_app(authed_settings, start_background=False)

    with TestClient(app) as client:
        external = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "password",
                "next": "https://example.com/phishing",
            },
            follow_redirects=False,
        )
        unexpected = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "password",
                "next": "/unexpected",
            },
            follow_redirects=False,
        )
        activity = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "password",
                "next": "/activity/42",
            },
            follow_redirects=False,
        )

    assert external.headers["location"] == "/"
    assert unexpected.headers["location"] == "/"
    assert activity.headers["location"] == "/activity/42"


def test_security_headers(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_csrf_required_for_protected_posts(settings):
    authed_settings = settings.__class__(**{**settings.__dict__, "web_auth_enabled": True})
    app = create_app(authed_settings, start_background=False)
    with TestClient(app) as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "password", "next": "/config"},
            follow_redirects=False,
        )
        response = client.post("/config/test-garmin", follow_redirects=False)

    assert response.status_code == 403


def test_igpsport_test_and_sync_require_csrf(settings):
    authed_settings = settings.__class__(
        **{
            **settings.__dict__,
            "web_auth_enabled": True,
            "igpsport_source_enabled": True,
        }
    )
    app = create_app(authed_settings, start_background=False)
    with TestClient(app) as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "password", "next": "/config"},
            follow_redirects=False,
        )
        test_response = client.post(
            "/config/source/igpsport/test",
            follow_redirects=False,
        )
        sync_response = client.post(
            "/config/source/igpsport/sync",
            follow_redirects=False,
        )

    assert test_response.status_code == 403
    assert sync_response.status_code == 403


def test_config_page_shows_auth_paths(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/config")

    assert response.status_code == 200
    assert 'src="/static/fit-to-garmin-bridge-logo.svg"' in response.text
    assert "navbar-expand-sm" in response.text
    assert "navbar-toggler" in response.text
    assert '<a class="nav-link" href="/health">Health</a>' not in response.text
    assert "Dropbox" in response.text
    assert "Sign up for Dropbox" in response.text
    assert "https://www.dropbox.com/register" in response.text
    assert "Garmin Upload" in response.text
    assert "Setup incomplete" in response.text
    assert "Dropbox still needs to be configured." in response.text
    assert "Garmin Upload still needs to be configured." in response.text
    assert "Web Security" in response.text
    assert "Garmin profile" in response.text
    assert "Garmin device target" in response.text
    assert "Use manual values below" in response.text
    assert "Identify Garmin Device" in response.text
    assert "Garmin Account and Upload Profile" in response.text
    assert "Garmin Session Upload" in response.text
    assert "iGPSPORT: Disabled" in response.text
    assert 'src="/static/config-source-status.js"' in response.text
    assert 'id="igpsport_username"' not in response.text


def test_config_page_shows_igpsport_only_when_enabled(settings):
    enabled_settings = replace(settings, igpsport_source_enabled=True)
    app = create_app(enabled_settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/config")

    assert response.status_code == 200
    assert "iGPSPORT: Needs setup" in response.text
    assert "iGPSPORT: Disabled" not in response.text
    assert 'id="igpsport_username"' in response.text
    assert "Account region" in response.text
    assert "International" in response.text
    assert "China" in response.text


def test_igpsport_profile_rejects_unsupported_api_host(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.post(
            "/config/igpsport/save",
            data={
                "igpsport_username": "account@example.test",
                "igpsport_password": "not-a-real-password",
                "igpsport_base_url": "https://example.test/service",
                "igpsport_import_mode": "new_only",
            },
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Select a supported iGPSPORT account region." in response.text
    assert not IGPSportStore(settings.igpsport_config_dir).profile_path.exists()


def test_config_never_renders_igpsport_password_or_token(settings):
    settings = replace(settings, igpsport_source_enabled=True)
    store = IGPSportStore(settings.igpsport_config_dir)
    store.save_profile(
        username="masked@example.test",
        password="profile-secret-value",
        base_url=settings.igpsport_base_url,
        import_mode="new_only",
    )
    store.save_session("session-secret-value")
    app = create_app(settings, start_background=False)

    with TestClient(app) as client:
        response = client.get("/config")

    assert response.status_code == 200
    assert "masked@example.test" in response.text
    assert "profile-secret-value" not in response.text
    assert "session-secret-value" not in response.text
    assert "Saved - leave blank" in response.text
    assert "saved Garmin session token" in response.text
    assert "Create Garmin Session" in response.text


def test_navbar_logo_asset_is_served(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/static/fit-to-garmin-bridge-logo.svg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"


def test_config_source_status_script_is_served(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/static/config-source-status.js")

    assert response.status_code == 200
    assert "text-bg-danger" in response.text
    assert "Disabled" in response.text


def test_sync_dropbox_to_incoming_copies_fit_files(settings, monkeypatch):
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rclone_config_path.write_text("[dropbox]\ntype = dropbox\n")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "lsjson":
            return SimpleNamespace(
                returncode=0,
                stdout='[{"Name":"ride.fit","Size":3,"IsDir":false}]',
                stderr="",
            )
        settings.incoming_dir.mkdir(parents=True, exist_ok=True)
        (settings.incoming_dir / "ride.fit").write_bytes(b"fit")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.setup_status.shutil.which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr("app.setup_status.subprocess.run", fake_run)

    result = sync_dropbox_to_incoming(settings)

    assert result.ok
    assert "copied 1 new FIT file" in result.output
    assert (settings.incoming_dir / "ride.fit").exists()
    assert "ride.fit" in (settings.incoming_dir.parent / ".last-rclone-copy").read_text()
    assert [command[1] for command in calls] == ["lsjson", "copy"]


def test_sync_dropbox_skips_files_already_recorded(settings, monkeypatch):
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rclone_config_path.write_text("[dropbox]\ntype = dropbox\n")
    db = Database(settings.sqlite_path)
    db.init()
    db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/uploaded/ride.fit",
        filename="ride.fit",
        sha256="known",
        file_size=3,
        activity_start_time="2026-01-01T00:00:00Z",
        status="uploaded",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='[{"Name":"ride.fit","Size":3,"IsDir":false}]',
            stderr="",
        )

    monkeypatch.setattr("app.setup_status.shutil.which", lambda _name: "/usr/bin/rclone")
    monkeypatch.setattr("app.setup_status.subprocess.run", fake_run)

    result = sync_dropbox_to_incoming(settings)

    assert result.ok
    assert "copied 0 new FIT file" in result.output
    assert "skipped 1 already handled file" in result.output
    assert [command[1] for command in calls] == ["lsjson"]


def test_sync_dropbox_skips_separator_variant_already_recorded(settings, monkeypatch):
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rclone_config_path.write_text("[dropbox]\ntype = dropbox\n")
    db = Database(settings.sqlite_path)
    db.init()
    db.create_activity(
        source_path="/data/incoming/ride-ELEMNT ABCD.fit",
        current_path="/data/uploaded/ride-ELEMNT ABCD.fit",
        filename="ride-ELEMNT ABCD.fit",
        sha256="known",
        file_size=3,
        activity_start_time="2026-01-01T00:00:00Z",
        status="uploaded",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='[{"Name":"ride-ELEMNT_ABCD.fit","Size":3,"IsDir":false}]',
            stderr="",
        )

    monkeypatch.setattr("app.setup_status.shutil.which", lambda _name: "/usr/bin/rclone")
    monkeypatch.setattr("app.setup_status.subprocess.run", fake_run)

    result = sync_dropbox_to_incoming(settings)

    assert result.ok
    assert "copied 0 new FIT file" in result.output
    assert "skipped 1 already handled file" in result.output
    assert [command[1] for command in calls] == ["lsjson"]


def test_delete_dropbox_source_uses_rclone_deletefile(settings, monkeypatch):
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rclone_config_path.write_text("[dropbox]\ntype = dropbox\n")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.setup_status.shutil.which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr("app.setup_status.subprocess.run", fake_run)

    result = delete_dropbox_source(settings, "ride.fit")

    assert result.ok
    assert commands == [
        [
            "rclone",
            "deletefile",
            "dropbox:Apps/WahooFitness/ride.fit",
            "--config",
            str(settings.rclone_config_path),
        ]
    ]
    assert "Deleted dropbox:Apps/WahooFitness/ride.fit from Dropbox." in result.output


def test_delete_dropbox_source_rejects_unsafe_names(settings, monkeypatch):
    settings.rclone_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rclone_config_path.write_text("[dropbox]\ntype = dropbox\n")
    monkeypatch.setattr("app.setup_status.shutil.which", lambda name: "/usr/bin/rclone")

    result = delete_dropbox_source(settings, "../ride.fit")

    assert not result.ok
    assert "unsafe Dropbox filename" in result.output


def test_rescan_pulls_dropbox_before_scanning(settings, monkeypatch):
    calls: list[str] = []

    def fake_sync(*, manual=False):
        assert manual
        calls.append("sync")
        return {
            "dropbox": SourceSyncResult(
                True,
                "Dropbox sync",
                "Copied 1 new FIT file from Dropbox.",
                downloaded=1,
            )
        }

    def fake_scan_once(self):
        calls.append("scan")
        return {"discovered": 1, "processed": 1}

    monkeypatch.setattr("app.jobs.BridgeService.scan_once", fake_scan_once)

    app = create_app(settings, start_background=False)
    monkeypatch.setattr(app.state.source_manager, "sync_all", fake_sync)
    with TestClient(app) as client:
        response = client.post("/rescan", follow_redirects=True)

    assert response.status_code == 200
    assert calls == ["sync", "scan"]
    assert "Copied 1 new FIT file from Dropbox." in response.text
    assert "Discovered 1 file(s). Processed 1 file(s)." in response.text


def test_confirm_imported_history(settings):
    app = create_app(settings, start_background=False)
    db = app.state.db
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

    with TestClient(app) as client:
        response = client.post("/activities/confirm-imported", follow_redirects=True)

    assert response.status_code == 200
    assert "Marked 1 imported activity file(s) as already on Garmin." in response.text
    assert db.get_activity(activity["id"])["status"] == "already_on_garmin"


def test_reprocess_direct_get_redirects_to_activity(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.get("/activity/1/reprocess", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/activity/1"


def test_reprocess_lock_returns_activity_warning(settings, monkeypatch):
    def locked_retry(self, activity_id, reset_retries=False):
        raise RuntimeError("Another scan or upload is already running")

    monkeypatch.setattr("app.jobs.BridgeService.retry_now", locked_retry)

    app = create_app(settings, start_background=False)
    app.state.db.init()
    app.state.db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/failed/ride.fit",
        filename="ride.fit",
        sha256="locked-retry",
        file_size=12,
        activity_start_time=None,
        status="failed",
    )
    with TestClient(app) as client:
        response = client.post("/activity/1/reprocess", follow_redirects=True)

    assert response.status_code == 200
    assert "Another scan or upload is already running" in response.text


def test_failed_activity_can_be_deleted_from_dropbox(settings, monkeypatch):
    def fake_delete(self, external_id):
        assert external_id == "ride.fit"
        return SourceResult(True, "Dropbox delete", "Deleted from Dropbox.")

    monkeypatch.setattr("app.sources.dropbox.DropboxSource.delete_remote_activity", fake_delete)

    app = create_app(settings, start_background=False)
    app.state.db.init()
    activity = app.state.db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/failed/ride.fit",
        filename="ride.fit",
        sha256="dropbox-delete",
        file_size=12,
        activity_start_time=None,
        status="failed",
    )

    with TestClient(app) as client:
        dashboard = client.get("/")
        detail = client.get(f"/activity/{activity['id']}")
        response = client.post(
            f"/activity/{activity['id']}/delete-dropbox",
            data={"next": "/", "csrf_token": ""},
            follow_redirects=True,
        )

    updated = app.state.db.get_activity(activity["id"])
    assert dashboard.status_code == 200
    assert f"/activity/{activity['id']}/delete-dropbox" in dashboard.text
    assert detail.status_code == 200
    assert "Delete source file" in detail.text
    assert response.status_code == 200
    assert "Deleted from Dropbox." in response.text
    assert updated is not None
    assert updated["status"] == "dropbox_deleted"
    assert updated["garmin_upload_status"] == "dropbox_deleted"


def test_dropbox_delete_rejects_unsafe_redirect(settings, monkeypatch):
    def fake_delete(_settings, _filename):
        return CommandResult(True, "Dropbox delete", "Deleted from Dropbox.")

    monkeypatch.setattr("app.jobs.delete_dropbox_source", fake_delete)

    app = create_app(settings, start_background=False)
    app.state.db.init()
    activity = app.state.db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/failed/ride.fit",
        filename="ride.fit",
        sha256="dropbox-delete-unsafe-redirect",
        file_size=12,
        activity_start_time=None,
        status="failed",
    )

    with TestClient(app) as client:
        response = client.post(
            f"/activity/{activity['id']}/delete-dropbox",
            data={"next": "//example.com/phishing", "csrf_token": ""},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_uploaded_activity_does_not_show_dropbox_delete(settings):
    app = create_app(settings, start_background=False)
    app.state.db.init()
    activity = app.state.db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/uploaded/ride.fit",
        filename="ride.fit",
        sha256="uploaded-no-delete",
        file_size=12,
        activity_start_time=None,
        status="uploaded",
    )

    with TestClient(app) as client:
        response = client.get(f"/activity/{activity['id']}")

    assert response.status_code == 200
    assert "Delete source file" not in response.text


def test_igpsport_activity_never_shows_remote_delete(settings):
    app = create_app(settings, start_background=False)
    app.state.db.init()
    activity = app.state.db.create_activity(
        source_path="/data/incoming/igpsport_1.fit",
        current_path="/data/failed/igpsport_1.fit",
        filename="igpsport_1.fit",
        sha256="igpsport-delete-hidden",
        file_size=12,
        activity_start_time=None,
        source_type="igpsport",
        source_external_id="1",
        source_display_name="iGPSPORT Cloud",
        status="failed",
    )

    with TestClient(app) as client:
        response = client.get(f"/activity/{activity['id']}")

    assert response.status_code == 200
    assert "iGPSPORT Cloud" in response.text
    assert "delete-dropbox" not in response.text


def test_activity_page_hides_stored_raw_garmin_login_trace(settings):
    app = create_app(settings, start_background=False)
    app.state.db.init()
    activity = app.state.db.create_activity(
        source_path="/data/incoming/ride.fit",
        current_path="/data/failed/ride.fit",
        filename="ride.fit",
        sha256="stored-raw-garmin-trace",
        file_size=12,
        activity_start_time=None,
        status="failed",
        error_message="GarminConnectAuthenticationError: MFA Required but no prompt_mfa mechanism supplied",
        garmin_response="GarminConnectAuthenticationError: MFA Required but no prompt_mfa mechanism supplied",
    )

    with TestClient(app) as client:
        response = client.get(f"/activity/{activity['id']}")

    assert response.status_code == 200
    assert "saved Garmin token" in response.text
    assert "prompt_mfa" not in response.text
    assert "GarminConnectAuthenticationError" not in response.text


def test_garmin_pause_can_be_cleared_from_config(settings):
    record_garmin_rate_limit(settings, "Mobile login returned 429 - IP rate limited by Garmin")
    app = create_app(settings, start_background=False)

    with TestClient(app) as client:
        response = client.post("/config/garmin/clear-pause", follow_redirects=True)

    assert response.status_code == 200
    assert active_garmin_cooldown(settings) is None
    assert "Cleared the local Garmin login pause" in response.text


def test_config_save_writes_runtime_config(settings):
    app = create_app(settings, start_background=False)
    original_db = app.state.db
    original_service = app.state.service
    with TestClient(app) as client:
        response = client.post(
            "/config/save",
            data={
                "rclone_remote": "dropbox",
                "dropbox_wahoo_path": "Apps/WahooFitness",
                "garmin_profile_name": "wahoo",
                "garmin_unit_id": "unit-123",
                "dry_run": "on",
            },
            follow_redirects=False,
        )
        config_response = client.get("/config")

    assert response.status_code == 303
    assert config_response.status_code == 200
    assert app.state.settings.garmin_unit_id == "unit-123"
    assert app.state.db is not original_db
    assert app.state.service is not original_service
    assert app.state.service.settings is app.state.settings
    assert "unit-123" in config_response.text
    written = settings.runtime_config_path.read_text()
    assert "DROPBOX_WAHOO_PATH=Apps/WahooFitness" in written
    assert "GARMIN_UNIT_ID=unit-123" in written
    assert settings.runtime_config_path.stat().st_mode & 0o777 == 0o600


def test_config_save_supports_both_activity_sources(settings):
    app = create_app(settings, start_background=False)
    scheduler_updates = []
    app.state.scheduler = SimpleNamespace(
        reconfigure=lambda manager, service: scheduler_updates.append((manager, service))
    )
    with TestClient(app) as client:
        response = client.post(
            "/config/save",
            data={
                "csrf_token": "",
                "rclone_remote": "dropbox",
                "dropbox_wahoo_path": "Apps/WahooFitness",
                "garmin_profile_name": "wahoo",
                "garmin_unit_id": "12345",
                "dropbox_source_enabled": "on",
                "igpsport_source_enabled": "on",
                "dropbox_poll_seconds": "60",
                "igpsport_poll_seconds": "900",
                "dry_run": "on",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert app.state.settings.dropbox_source_enabled
    assert app.state.settings.igpsport_source_enabled
    assert scheduler_updates == [(app.state.source_manager, app.state.service)]
    saved = settings.runtime_config_path.read_text()
    assert "DROPBOX_SOURCE_ENABLED=true" in saved
    assert "IGPSPORT_SOURCE_ENABLED=true" in saved


def test_dropbox_auth_save_writes_rclone_config(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.post(
            "/config/dropbox/save",
            data={
                "rclone_remote": "dropbox",
                "dropbox_wahoo_path": "Apps/WahooFitness",
                "rclone_token_json": '{"access_token":"abc","token_type":"bearer"}',
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    written = settings.rclone_config_path.read_text()
    assert "[dropbox]" in written
    assert "type = dropbox" in written
    assert '"access_token":"abc"' in written
    assert settings.rclone_config_path.stat().st_mode & 0o777 == 0o600


def test_garmin_profile_save_writes_native_config(settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        response = client.post(
            "/config/garmin/save",
            data={
                "garmin_username": "user@example.com",
                "garmin_password": "secret",
                "garmin_profile_name": "wahoo",
                "garmin_unit_id": "1234567890",
                "garmin_manufacturer_id": "1",
                "garmin_product_id": "3907",
                "garmin_software_version": "1200",
            },
            follow_redirects=False,
        )
        saved_page = client.get("/config")

    assert response.status_code == 303
    config_file = settings.garmin_config_dir / "profile.json"
    written = config_file.read_text()
    assert "user@example.com" in written
    assert "1234567890" in written
    assert '"name": "wahoo"' in written
    assert "user@example.com" in saved_page.text
    assert "Saved - leave blank to keep current password" in saved_page.text
    assert "value=\"3907\"" in saved_page.text
    assert "value=\"1234567890\"" in saved_page.text

    check = run_garmin_upload_test(settings)
    assert check.ok
    assert "Garmin FIT SDK rewrite is available." in check.output
    assert "Profile: wahoo" in check.output
    assert "Garmin account: user@example.com" in check.output
    assert "usage:" not in check.output
