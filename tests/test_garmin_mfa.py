from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db import Database
from app.garmin_cli import main as cli_main
from app.garmin_upload import (
    GarminMFAPending,
    complete_garmin_mfa_session,
    start_garmin_session_login,
)
from app.jobs import BridgeService
from app.main import create_app


def test_start_garmin_session_login_mfa_required(settings, tmp_path):
    mock_client = MagicMock()
    mock_client.login.return_value = ("MFA_REQUIRED", None)

    with patch("garminconnect.Garmin", return_value=mock_client):
        ok, status, pending = start_garmin_session_login(
            settings, "rider@example.com", "secretpass"
        )
        assert ok
        assert status == "MFA_REQUIRED"
        assert isinstance(pending, GarminMFAPending)
        assert pending.username == "rider@example.com"


def test_start_garmin_session_login_success_without_mfa(settings, tmp_path):
    mock_client = MagicMock()
    mock_client.login.return_value = (None, None)

    with patch("garminconnect.Garmin", return_value=mock_client):
        ok, status, pending = start_garmin_session_login(
            settings, "rider@example.com", "secretpass"
        )
        assert ok
        assert pending is None
        assert "Garmin session created" in status


def test_complete_garmin_mfa_session_success(settings, tmp_path):
    mock_garmin = MagicMock()
    mock_garmin.client._complete_mfa.return_value = None
    mock_garmin._load_profile_and_settings.return_value = None

    token_dir = tmp_path / "tokens"
    pending = GarminMFAPending(
        client=mock_garmin,
        username="rider@example.com",
        token_dir=token_dir,
    )

    ok, msg = complete_garmin_mfa_session(pending, "123456", settings)
    assert ok
    assert "successful" in msg
    mock_garmin.client._complete_mfa.assert_called_once_with("123456")
    mock_garmin.client.dump.assert_called_once_with(str(token_dir))


def test_complete_garmin_mfa_session_empty_code(settings, tmp_path):
    mock_garmin = MagicMock()
    token_dir = tmp_path / "tokens"
    pending = GarminMFAPending(
        client=mock_garmin,
        username="rider@example.com",
        token_dir=token_dir,
    )

    ok, msg = complete_garmin_mfa_session(pending, "", settings)
    assert not ok
    assert "cannot be empty" in msg


def test_web_routes_garmin_mfa_flow(settings):
    db = Database(settings.sqlite_path)
    db.init()
    service = BridgeService(settings, db)
    service.setup()
    app = create_app(settings, start_background=False)
    app.state.db = db
    app.state.service = service
    client = TestClient(app)

    mock_garmin = MagicMock()
    mock_garmin.login.return_value = ("MFA_REQUIRED", None)
    mock_garmin.client._complete_mfa.return_value = None

    with patch("garminconnect.Garmin", return_value=mock_garmin):
        # 1. Save Garmin credentials triggering MFA
        resp = client.post(
            "/config/garmin/save",
            data={
                "garmin_username": "rider@example.com",
                "garmin_password": "secretpassword",
                "garmin_profile_name": "wahoo",
                "garmin_unit_id": "3991000001",
                "detected_device_id": "edge_1040",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Garmin Two-Factor Authentication" in resp.text
        assert "garmin-mfa-box" in resp.text
        assert app.state.garmin_mfa_pending is not None

        # 2. Complete MFA
        resp2 = client.post(
            "/config/garmin/mfa/complete",
            data={"mfa_code": "654321"},
            follow_redirects=True,
        )
        assert resp2.status_code == 200
        assert "successful" in resp2.text
        assert app.state.garmin_mfa_pending is None


def test_web_routes_garmin_mfa_cancel(settings):
    db = Database(settings.sqlite_path)
    db.init()
    service = BridgeService(settings, db)
    service.setup()
    app = create_app(settings, start_background=False)
    app.state.db = db
    app.state.service = service
    client = TestClient(app)

    app.state.garmin_mfa_pending = GarminMFAPending(
        client=MagicMock(),
        username="rider@example.com",
        token_dir=settings.garmin_config_dir / "tokens",
    )

    resp = client.post("/config/garmin/mfa/cancel", follow_redirects=True)
    assert resp.status_code == 200
    assert "Cancelled" in resp.text
    assert app.state.garmin_mfa_pending is None


def test_garmin_cli_main_success(settings, monkeypatch):
    monkeypatch.setenv("GARMIN_USERNAME", "rider@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "secretpassword")

    mock_client = MagicMock()
    mock_client.login.return_value = (None, None)

    with patch("garminconnect.Garmin", return_value=mock_client):
        code = cli_main(settings)
        assert code == 0
        mock_client.login.assert_called_once()
