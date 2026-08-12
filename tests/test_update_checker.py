from __future__ import annotations

import time
from unittest.mock import patch, MagicMock
import httpx
import pytest
from fastapi.testclient import TestClient

from app.update_checker import (
    UpdateStatus,
    _parse_version,
    _compare_versions,
    check_for_update,
    clear_cache,
    CACHE_TTL_SECONDS,
    GITHUB_REPO,
)
from app.version import get_app_version
from app.main import create_app


# Setup cache clearing fixture
@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield


### Version parsing tests:

def test_parse_version_standard():
    assert _parse_version("1.2.0") == (1, 2, 0)

def test_parse_version_with_v_prefix():
    assert _parse_version("v1.2.0") == (1, 2, 0)

def test_parse_version_invalid():
    assert _parse_version("not-a-version") is None

def test_parse_version_empty():
    assert _parse_version("") is None


### Version comparison tests:

def test_version_equal():
    assert not _compare_versions("1.2.0", "1.2.0")

def test_version_newer_patch():
    assert _compare_versions("1.2.0", "1.2.1")

def test_version_newer_minor():
    assert _compare_versions("1.2.9", "1.3.0")

def test_version_newer_major():
    assert _compare_versions("1.99.99", "2.0.0")

def test_version_10_vs_9():
    assert _compare_versions("1.9.0", "1.10.0")

def test_version_older():
    assert not _compare_versions("2.0.0", "1.0.0")

def test_version_compare_invalid():
    assert not _compare_versions("1.0.0", "invalid")


### check_for_update tests:

def _mock_httpx_client(responses, raise_err=None):
    mock_client = MagicMock()
    mock_ctx = MagicMock()
    
    mock_client.__enter__.return_value = mock_ctx
    
    if raise_err:
        mock_ctx.get.side_effect = raise_err
    else:
        mock_response = MagicMock()
        mock_response.json.return_value = responses
        mock_response.raise_for_status.return_value = None
        mock_ctx.get.return_value = mock_response

    return mock_client


@patch("app.update_checker.httpx.Client")
def test_valid_newer_release(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([
        {"tag_name": "v1.3.0", "draft": False, "prerelease": False, "html_url": "https://github.com/ntrance/wahoo-garmin-fit-bridge/releases/tag/v1.3.0"},
        {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "html_url": "https://github.com/ntrance/wahoo-garmin-fit-bridge/releases/tag/v1.2.0"}
    ])

    status = check_for_update("1.2.0")
    
    assert status.update_available is True
    assert status.latest_version == "1.3.0"
    assert status.release_url == f"https://github.com/{GITHUB_REPO}/releases/tag/v1.3.0"
    assert status.error is None
    assert status.checked_at > 0


@patch("app.update_checker.httpx.Client")
def test_current_is_latest(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([
        {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "html_url": "https://github.com/ntrance/wahoo-garmin-fit-bridge/releases/tag/v1.2.0"}
    ])

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.latest_version == "1.2.0"
    assert status.error is None


@patch("app.update_checker.httpx.Client")
def test_api_unavailable(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([], raise_err=httpx.ConnectError("Connection failed"))

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.error is not None
    assert "Connection failed" in status.error


@patch("app.update_checker.httpx.Client")
def test_api_timeout(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([], raise_err=httpx.ReadTimeout("Timeout"))

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.error is not None
    assert "Timeout" in status.error


@patch("app.update_checker.httpx.Client")
def test_malformed_json(mock_client_class):
    mock_client = MagicMock()
    mock_ctx = MagicMock()
    mock_client.__enter__.return_value = mock_ctx
    
    mock_response = MagicMock()
    mock_response.json.side_effect = ValueError("Invalid JSON")
    mock_response.raise_for_status.return_value = None
    mock_ctx.get.return_value = mock_response

    mock_client_class.return_value = mock_client

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.error is not None


@patch("app.update_checker.httpx.Client")
def test_malformed_version_in_release(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([
        {"tag_name": "invalid", "draft": False, "prerelease": False, "html_url": "..."}
    ])

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.error is None


@patch("app.update_checker.httpx.Client")
def test_draft_release_skipped(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([
        {"tag_name": "v2.0.0", "draft": True, "prerelease": False, "html_url": "..."},
        {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "html_url": "..."}
    ])

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.latest_version == "1.2.0"


@patch("app.update_checker.httpx.Client")
def test_prerelease_skipped(mock_client_class):
    mock_client_class.return_value = _mock_httpx_client([
        {"tag_name": "v2.0.0-beta", "draft": False, "prerelease": True, "html_url": "..."},
        {"tag_name": "v1.2.0", "draft": False, "prerelease": False, "html_url": "..."}
    ])

    status = check_for_update("1.2.0")
    
    assert status.update_available is False
    assert status.latest_version == "1.2.0"


@patch("app.update_checker.httpx.Client")
def test_cached_response(mock_client_class):
    mock_client = _mock_httpx_client([
        {"tag_name": "v1.3.0", "draft": False, "prerelease": False, "html_url": "..."}
    ])
    mock_client_class.return_value = mock_client

    status1 = check_for_update("1.2.0")
    assert status1.update_available is True
    
    # Should use cache, so call count on HTTP get should be 1
    mock_ctx = mock_client.__enter__.return_value
    assert mock_ctx.get.call_count == 1
    
    status2 = check_for_update("1.2.0")
    assert status2.update_available is True
    assert mock_ctx.get.call_count == 1


@patch("app.update_checker.httpx.Client")
@patch("app.update_checker.time.time")
def test_cache_expired(mock_time, mock_client_class):
    mock_time.return_value = 1000.0

    mock_client = _mock_httpx_client([
        {"tag_name": "v1.3.0", "draft": False, "prerelease": False, "html_url": "..."}
    ])
    mock_client_class.return_value = mock_client

    status1 = check_for_update("1.2.0")
    assert status1.update_available is True
    
    mock_ctx = mock_client.__enter__.return_value
    assert mock_ctx.get.call_count == 1
    
    # Advance time past cache TTL
    mock_time.return_value = 1000.0 + CACHE_TTL_SECONDS + 10.0

    status2 = check_for_update("1.2.0")
    assert status2.update_available is True
    assert mock_ctx.get.call_count == 2


### get_app_version tests:

def test_get_app_version_from_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.5.0")
    assert get_app_version() == "1.5.0"


def test_get_app_version_dev_fallback(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    with patch("importlib.metadata.version", side_effect=Exception("not found")):
        assert get_app_version() == "dev"


def test_get_app_version_from_metadata(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    with patch("importlib.metadata.version", return_value="1.6.0"):
        assert get_app_version() == "1.6.0"


### Dashboard integration tests:

def test_dashboard_shows_version_footer(settings):
    app = create_app(settings, start_background=False)
    with patch("app.main.get_app_version", return_value="1.2.3"):
        with TestClient(app) as client:
            response = client.get("/")

    assert response.status_code == 200
    assert "FIT to Garmin Bridge v" in response.text


def test_dashboard_shows_update_notification(settings):
    app = create_app(settings, start_background=False)
    
    # Mock update status as update available
    status = UpdateStatus(
        latest_version="1.3.0",
        update_available=True,
        release_url="https://github.com/ntrance/wahoo-garmin-fit-bridge/releases/tag/v1.3.0",
        checked_at=time.time(),
        error=None
    )
    app.state.update_status = status
    
    with TestClient(app) as client:
        response = client.get("/")
        
    assert response.status_code == 200
    assert "update is available" in response.text.lower() or "new version" in response.text.lower() or "v1.3.0" in response.text


def test_dashboard_no_notification_when_up_to_date(settings):
    app = create_app(settings, start_background=False)
    
    # Mock update status as not available
    status = UpdateStatus(
        latest_version="1.2.0",
        update_available=False,
        release_url=None,
        checked_at=time.time(),
        error=None
    )
    app.state.update_status = status
    
    with TestClient(app) as client:
        response = client.get("/")
        
    assert response.status_code == 200
    # Notification shouldn't be there, hard to test absence unless we know exact text, 
    # but at least it should return 200.
    # Assuming "Update available" alert text
    assert "update is available" not in response.text.lower()


def test_dashboard_works_without_update_status(settings):
    app = create_app(settings, start_background=False)
    
    app.state.update_status = None
    
    with TestClient(app) as client:
        response = client.get("/")
        
    assert response.status_code == 200
