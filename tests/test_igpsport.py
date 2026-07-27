from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.db import Database
from app.sources.igpsport import (
    IGPSportActivity,
    IGPSportClient,
    IGPSportError,
    IGPSportSource,
    IGPSportStore,
)
from app.settings import IGPSPORT_DEFAULT_BASE_URL


def _client(tmp_path: Path, handler) -> tuple[IGPSportClient, IGPSportStore]:
    store = IGPSportStore(tmp_path / "igpsport")
    store.save_profile(
        username="account@example.test",
        password="not-a-real-password",
        base_url=IGPSPORT_DEFAULT_BASE_URL,
        import_mode="new_only",
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        IGPSportClient(
            store,
            base_url=IGPSPORT_DEFAULT_BASE_URL,
            client=http_client,
        ),
        store,
    )


def test_successful_login_and_private_session_file(tmp_path):
    def handler(request):
        assert request.url.path.endswith("/auth/account/login")
        return httpx.Response(200, json={"data": {"accessToken": "session-token"}})

    client, store = _client(tmp_path, handler)

    assert client.login() == "session-token"
    assert store.load_session()["access_token"] == "session-token"
    assert store.session_path.stat().st_mode & 0o777 == 0o600


def test_existing_token_is_reused_without_login(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"rows": []}})

    client, store = _client(tmp_path, handler)
    store.save_session("existing-token")

    assert client.list_activities() == []
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer existing-token"


def test_reauthenticates_once_after_rejected_token(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/auth/account/login"):
            return httpx.Response(200, json={"accessToken": "fresh-token"})
        if request.headers.get("authorization") == "Bearer stale-token":
            return httpx.Response(401)
        return httpx.Response(200, json={"rows": [{"rideId": "42"}]})

    client, store = _client(tmp_path, handler)
    store.save_session("stale-token")

    assert client.list_activities()[0].ride_id == "42"
    assert len(calls) == 3
    assert store.load_session()["access_token"] == "fresh-token"


@pytest.mark.parametrize("status_code", [401, 403])
def test_invalid_credentials_require_attention(tmp_path, status_code):
    client, _ = _client(
        tmp_path,
        lambda request: httpx.Response(status_code),
    )

    with pytest.raises(IGPSportError) as exc_info:
        client.login()

    assert exc_info.value.requires_attention
    assert "password" not in str(exc_info.value).lower()


def test_structured_login_rejection_explains_credentials_and_region(tmp_path):
    client, _ = _client(
        tmp_path,
        lambda request: httpx.Response(
            403,
            json={"code": 1002, "message": "Password error"},
        ),
    )

    with pytest.raises(IGPSportError) as exc_info:
        client.login()

    message = str(exc_info.value).lower()
    assert "account identifier or password" in message
    assert "region" in message


def test_http_429_honours_retry_after(tmp_path):
    client, _ = _client(
        tmp_path,
        lambda request: httpx.Response(429, headers={"Retry-After": "120"}),
    )

    with pytest.raises(IGPSportError) as exc_info:
        client.login()

    assert exc_info.value.retry_after_seconds == 120


def test_server_errors_use_bounded_retries(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client, _ = _client(tmp_path, handler)

    with pytest.raises(IGPSportError):
        client.login()

    assert calls == 3


def test_invalid_json_and_missing_access_token_are_rejected(tmp_path):
    invalid_client, _ = _client(
        tmp_path / "invalid",
        lambda request: httpx.Response(200, text="<html>no json</html>"),
    )
    with pytest.raises(IGPSportError, match="invalid JSON"):
        invalid_client.login()

    missing_client, _ = _client(
        tmp_path / "missing",
        lambda request: httpx.Response(200, json={"data": {"success": True}}),
    )
    with pytest.raises(IGPSportError, match="no access token"):
        missing_client.login()


def test_activity_and_download_url_parsing(tmp_path):
    def handler(request):
        if request.url.path.endswith("/auth/account/login"):
            return httpx.Response(200, json={"data": {"accessToken": "token"}})
        if "queryMyActivity" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "records": [
                            {
                                "rideId": 123,
                                "startTime": "2026-07-27T08:00:00Z",
                                "fileName": "ride.fit",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "Success",
                "data": "https://downloads.example.test/ride.fit",
            },
        )

    client, _ = _client(tmp_path, handler)

    activities = client.list_activities()
    assert activities == [
        IGPSportActivity("123", "2026-07-27T08:00:00Z", "ride.fit")
    ]
    assert client.get_download_url("123") == "https://downloads.example.test/ride.fit"


def test_invalid_download_url_is_rejected(tmp_path):
    def handler(request):
        if request.url.path.endswith("/auth/account/login"):
            return httpx.Response(200, json={"accessToken": "token"})
        return httpx.Response(200, json={"downloadUrl": "http://unsafe.example/ride.fit"})

    client, _ = _client(tmp_path, handler)
    with pytest.raises(IGPSportError, match="invalid FIT download URL"):
        client.get_download_url("123")


def test_successful_fit_download_is_atomic(tmp_path, monkeypatch):
    fit_bytes = b"\x0e\x20\x00\x00\x00\x00\x00\x00.FITpayload"

    def handler(request):
        if request.url.path.endswith("/auth/account/login"):
            return httpx.Response(200, json={"accessToken": "token"})
        if "getDownloadUrl" in request.url.path:
            return httpx.Response(
                200,
                json={"downloadUrl": "https://downloads.example.test/ride.fit"},
            )
        return httpx.Response(200, content=fit_bytes)

    client, _ = _client(tmp_path, handler)
    monkeypatch.setattr("app.sources.igpsport.validate_fit_file", lambda path: None)

    result = client.download_fit("abc-123", tmp_path / "incoming", 1024)

    assert result.name == "igpsport_abc-123.fit"
    assert result.read_bytes() == fit_bytes
    assert not list((tmp_path / "incoming").glob("*.tmp"))


def test_html_and_oversized_downloads_are_rejected_and_cleaned(tmp_path):
    responses = [
        httpx.Response(200, json={"accessToken": "token"}),
        httpx.Response(
            200,
            json={"downloadUrl": "https://downloads.example.test/ride.fit"},
        ),
        httpx.Response(200, content=b"<html>login</html>"),
    ]
    client, _ = _client(tmp_path / "html", lambda request: responses.pop(0))
    with pytest.raises(IGPSportError, match="web or JSON"):
        client.download_fit("1", tmp_path / "html-incoming", 1024)
    assert not list((tmp_path / "html-incoming").glob("*.tmp"))

    oversized_responses = [
        httpx.Response(200, json={"accessToken": "token"}),
        httpx.Response(
            200,
            json={"downloadUrl": "https://downloads.example.test/ride.fit"},
        ),
        httpx.Response(200, content=b"x" * 100, headers={"Content-Length": "100"}),
    ]
    oversized, _ = _client(
        tmp_path / "oversized",
        lambda request: oversized_responses.pop(0),
    )
    with pytest.raises(IGPSportError, match="size limit"):
        oversized.download_fit("2", tmp_path / "oversized-incoming", 50)
    assert not list((tmp_path / "oversized-incoming").glob("*.tmp"))


def test_blank_profile_password_preserves_saved_password(tmp_path):
    store = IGPSportStore(tmp_path / "igpsport")
    store.save_profile(
        username="first@example.test",
        password="saved-secret",
        base_url=IGPSPORT_DEFAULT_BASE_URL,
        import_mode="new_only",
    )

    store.save_profile(
        username="second@example.test",
        password="",
        base_url=IGPSPORT_DEFAULT_BASE_URL,
        import_mode="since_date",
        cutoff_date="2026-01-01",
    )

    assert store.load_profile()["password"] == "saved-secret"
    assert store.load_profile()["username"] == "second@example.test"


def test_profile_change_clears_saved_session(tmp_path):
    store = IGPSportStore(tmp_path / "igpsport")
    store.save_profile(
        username="first@example.test",
        password="saved-secret",
        base_url=IGPSPORT_DEFAULT_BASE_URL,
        import_mode="new_only",
    )
    store.save_session("stale-token")

    store.save_profile(
        username="second@example.test",
        password="",
        base_url=IGPSPORT_DEFAULT_BASE_URL,
        import_mode="new_only",
    )

    assert store.load_session() == {}


def test_source_errors_redact_profile_and_session_secrets(settings):
    db = Database(settings.sqlite_path)
    db.init()

    class FailingClient:
        def list_activities(self, page=1, page_size=20):
            raise IGPSportError(
                "Rejected account@example.test saved-secret session-secret"
            )

    source = IGPSportSource(settings, db, client=FailingClient())
    source.store.save_profile(
        username="account@example.test",
        password="saved-secret",
        base_url=settings.igpsport_base_url,
        import_mode="new_only",
    )
    source.store.save_session("session-secret")

    result = source.test_connection()

    assert not result.ok
    assert "account@example.test" not in result.message
    assert "saved-secret" not in result.message
    assert "session-secret" not in result.message
    assert result.message.count("[redacted]") == 3


def test_incremental_source_stops_at_known_id_and_downloads_unknown_once(
    settings,
    monkeypatch,
):
    db = Database(settings.sqlite_path)
    db.init()
    db.record_source_item(
        source_type="igpsport",
        source_external_id="known",
    )
    settings = replace(
        settings,
        igpsport_source_enabled=True,
        igpsport_import_mode="since_date",
    )
    calls = []

    class FakeClient:
        def list_activities(self, page=1, page_size=20):
            calls.append(("list", page))
            return [
                IGPSportActivity("new", "2026.07.27", "new.fit"),
                IGPSportActivity("known", "2026-07-26T10:00:00Z", "known.fit"),
                IGPSportActivity("older", "2026-07-25T10:00:00Z", "older.fit"),
            ]

        def download_fit(self, ride_id, incoming_dir, max_bytes):
            calls.append(("download", ride_id))
            path = incoming_dir / f"igpsport_{ride_id}.fit"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake.FIT.content")
            return path

    source = IGPSportSource(settings, db, client=FakeClient())
    source.store.save_profile(
        username="account@example.test",
        password="saved-secret",
        base_url=settings.igpsport_base_url,
        import_mode="since_date",
        cutoff_date="2026-01-01",
    )

    result = source.sync_to_incoming()

    assert result.downloaded == 1
    assert calls == [("list", 1), ("download", "new")]
    assert db.is_source_item_known("igpsport", "new")
    sidecar = json.loads(
        (settings.incoming_dir / "igpsport_new.fit.source.json").read_text()
    )
    assert sidecar["source_external_id"] == "new"
