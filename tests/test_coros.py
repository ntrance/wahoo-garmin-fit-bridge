from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.settings import COROS_DEFAULT_BASE_URL
from app.sources.base import read_source_sidecar
from app.sources.coros import (
    CorosClient,
    CorosError,
    CorosSource,
    CorosStore,
)


def _client(tmp_path: Path, handler) -> tuple[CorosClient, CorosStore]:
    store = CorosStore(tmp_path / "coros")
    store.save_profile(
        username="athlete@example.com",
        password="secret-password",
        base_url=COROS_DEFAULT_BASE_URL,
        import_mode="new_only",
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        CorosClient(
            store,
            base_url=COROS_DEFAULT_BASE_URL,
            client=http_client,
        ),
        store,
    )


def test_coros_login_sends_md5_and_saves_session(tmp_path):
    def handler(request):
        assert request.url.path.endswith("/account/login")
        body = json.loads(request.content)
        assert body["account"] == "athlete@example.com"
        expected_hash = hashlib.md5(b"secret-password").hexdigest()
        assert body["pwd"] == expected_hash
        assert body["accountType"] == 2
        return httpx.Response(
            200,
            json={
                "code": "0000",
                "message": "OK",
                "data": {
                    "accessToken": "coros-test-token",
                    "userId": "user-12345",
                    "expiresAt": "2099-01-01T00:00:00Z",
                },
            },
        )

    client, store = _client(tmp_path, handler)
    token = client.login()

    assert token == "coros-test-token"
    saved = store.load_session()
    assert saved["access_token"] == "coros-test-token"
    assert saved["user_id"] == "user-12345"
    assert store.session_path.stat().st_mode & 0o777 == 0o600


def test_coros_token_reused_without_login(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": "0000", "data": {"dataList": []}},
        )

    client, store = _client(tmp_path, handler)
    store.save_session("cached-token", expires_at="2099-01-01T00:00:00Z")

    activities = client.list_activities()
    assert activities == []
    assert len(requests) == 1
    assert requests[0].headers["accesstoken"] == "cached-token"


def test_coros_reauthenticates_after_expired_token(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("accesstoken")))
        if request.url.path.endswith("/account/login"):
            return httpx.Response(
                200,
                json={"code": "0000", "data": {"accessToken": "fresh-token"}},
            )
        if request.headers.get("accesstoken") == "expired-token":
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "code": "0000",
                "data": {
                    "dataList": [
                        {
                            "labelId": "coros-act-99",
                            "sportType": 100,
                            "startTime": 1723700000,
                            "name": "Morning Ride",
                        }
                    ]
                },
            },
        )

    client, store = _client(tmp_path, handler)
    store.save_session("expired-token")

    activities = client.list_activities()
    assert len(activities) == 1
    assert activities[0].label_id == "coros-act-99"
    assert activities[0].sport_type == 100
    assert activities[0].original_filename == "Morning Ride"
    assert store.load_session()["access_token"] == "fresh-token"


def test_coros_login_failure_raises_coros_error(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json={"code": "1001", "message": "Invalid password"},
        )

    client, _ = _client(tmp_path, handler)
    with pytest.raises(CorosError) as exc_info:
        client.login()
    assert "Invalid password" in str(exc_info.value)
    assert exc_info.value.requires_attention is True


def test_coros_download_fit_file(tmp_path):
    def handler(request):
        if request.url.path.endswith("/activity/detail/download"):
            body = json.loads(request.content)
            assert body["labelId"] == "act-555"
            assert body["sportType"] == 100
            assert body["fileType"] == "fit"
            return httpx.Response(
                200,
                json={
                    "code": "0000",
                    "data": {"fileUrl": "https://storage.coros.com/files/act-555.fit"},
                },
            )
        if str(request.url) == "https://storage.coros.com/files/act-555.fit":
            return httpx.Response(200, content=b".FIT_BINARY_DATA.")
        return httpx.Response(404)

    client, store = _client(tmp_path, handler)
    store.save_session("valid-token")
    incoming_dir = tmp_path / "incoming"

    fit_path = client.download_fit("act-555", 100, incoming_dir, 1024 * 1024)
    assert fit_path.exists()
    assert fit_path.name == "coros_act-555.fit"
    assert fit_path.read_bytes() == b".FIT_BINARY_DATA."


def test_coros_source_sync_and_sidecar(tmp_path, settings):
    incoming_dir = tmp_path / "incoming"
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    db.init()

    coros_settings = replace(
        settings,
        coros_source_enabled=True,
        coros_config_dir=tmp_path / "coros",
        incoming_dir=incoming_dir,
    )
    store = CorosStore(coros_settings.coros_config_dir)
    store.save_profile(
        username="user@coros.test",
        password="password123",
        base_url=COROS_DEFAULT_BASE_URL,
        import_mode="new_only",
    )
    # Simulate first sync already done so new activities will be downloaded
    store.save_state({"enabled_at": "2026-08-01T00:00:00Z"})
    store.save_session("coros-token")

    def handler(request):
        if request.url.path.endswith("/activity/query"):
            return httpx.Response(
                200,
                json={
                    "code": "0000",
                    "data": {
                        "dataList": [
                            {
                                "labelId": "ride-101",
                                "sportType": 100,
                                "startTime": 1785574800,  # 2026-08-10
                                "name": "Gravel Loop",
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/activity/detail/download"):
            return httpx.Response(
                200,
                json={"code": "0000", "data": {"fileUrl": "https://cdn.coros.com/ride-101.fit"}},
            )
        if str(request.url) == "https://cdn.coros.com/ride-101.fit":
            return httpx.Response(200, content=b".FIT_CONTENT.")
        return httpx.Response(404)

    mock_client = CorosClient(
        store,
        base_url=COROS_DEFAULT_BASE_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    source = CorosSource(coros_settings, db, store=store, client=mock_client)

    result = source.sync_to_incoming()
    assert result.ok is True
    assert result.downloaded == 1

    downloaded_fit = incoming_dir / "coros_ride-101.fit"
    assert downloaded_fit.exists()
    sidecar = read_source_sidecar(downloaded_fit)
    assert sidecar is not None
    assert sidecar.source_type == "coros"
    assert sidecar.source_external_id == "ride-101"
    assert sidecar.source_display_name == "COROS Cloud"
    assert sidecar.source_original_filename == "Gravel Loop"


def test_coros_web_routes(settings, tmp_path):
    coros_settings = replace(
        settings,
        coros_source_enabled=True,
        coros_config_dir=tmp_path / "coros",
        web_auth_enabled=False,
    )
    app = create_app(coros_settings, start_background=False)

    with TestClient(app) as client:
        # Save profile
        res = client.post(
            "/config/coros/save",
            data={
                "coros_username": "user@coros.com",
                "coros_password": "mypassword",
                "coros_base_url": COROS_DEFAULT_BASE_URL,
                "coros_import_mode": "new_only",
                "coros_cutoff_date": "",
            },
            follow_redirects=True,
        )
        assert res.status_code == 200
        store = CorosStore(coros_settings.coros_config_dir)
        profile = store.load_profile()
        assert profile["username"] == "user@coros.com"
        assert profile["password"] == "mypassword"

        # Clear session
        store.save_session("dummy-token")
        res = client.post("/config/coros/clear-session", follow_redirects=True)
        assert res.status_code == 200
        assert store.load_session() == {}

        # Delete profile
        res = client.post(
            "/config/coros/delete",
            data={"confirm": "DELETE"},
            follow_redirects=True,
        )
        assert res.status_code == 200
        assert store.load_profile() == {}
