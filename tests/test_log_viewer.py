from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import Database
from app.jobs import BridgeService
from app.log_viewer import (
    categorize_logger,
    filter_entries,
    get_logs_summary,
    parse_log_file,
    parse_log_text,
    purge_logs,
)
from app.main import create_app

SAMPLE_LOGS = """2026-07-25T15:33:41Z INFO [app.jobs] Removed repeated source file already recorded as activity 100: /data/incoming/test.fit
2026-08-06T02:39:03Z WARNING [app.source_scheduler] Timed out while listing Dropbox FIT files.
2026-08-02T13:16:17Z WARNING [garminconnect.client] mobile+cffi returned 429: Mobile login returned 429 - IP rate limited
2026-08-12T11:29:06Z INFO [httpx] HTTP Request: GET https://api.github.com/repos/ntrance/wahoo-garmin-fit-bridge/releases "HTTP/1.1 200 OK"
2026-08-15T12:00:00Z ERROR [app.jobs] Processing failed with traceback:
Traceback (most recent call last):
  File "test.py", line 1, in <module>
    raise ValueError("Test error")
2026-08-15T12:05:00Z INFO [uvicorn.access] 127.0.0.1 - "GET / HTTP/1.1" 200
"""


def test_parse_log_text():
    entries = parse_log_text(SAMPLE_LOGS)
    assert len(entries) == 6

    # 1. Job entry
    assert entries[0].logger == "app.jobs"
    assert entries[0].level == "INFO"
    assert entries[0].category == "jobs"
    assert "activity 100" in entries[0].message

    # 2. Source Scheduler entry
    assert entries[1].logger == "app.source_scheduler"
    assert entries[1].level == "WARNING"
    assert entries[1].category == "sources"

    # 3. Garmin entry
    assert entries[2].logger == "garminconnect.client"
    assert entries[2].level == "WARNING"
    assert entries[2].category == "garmin"

    # 4. HTTP entry
    assert entries[3].logger == "httpx"
    assert entries[3].level == "INFO"
    assert entries[3].category == "http"

    # 5. Multiline error entry
    assert entries[4].logger == "app.jobs"
    assert entries[4].level == "ERROR"
    assert "Traceback" in entries[4].message

    # 6. Web server entry
    assert entries[5].logger == "uvicorn.access"
    assert entries[5].level == "INFO"
    assert entries[5].category == "server"


def test_parse_log_file(tmp_path):
    log_path = tmp_path / "app.log"
    assert parse_log_file(log_path) == []

    log_path.write_text(SAMPLE_LOGS)
    entries = parse_log_file(log_path)
    assert len(entries) == 6
    assert entries[0].logger == "app.jobs"


def test_categorize_logger():
    cat, label, icon = categorize_logger("app.jobs", "Processing file")
    assert cat == "jobs"
    assert "Sync" in label

    cat, label, icon = categorize_logger("app.source_scheduler", "Polling")
    assert cat == "sources"

    cat, label, icon = categorize_logger("garminconnect", "Login")
    assert cat == "garmin"

    cat, label, icon = categorize_logger("httpx", "GET")
    assert cat == "http"

    cat, label, icon = categorize_logger("unknown.module", "Some event")
    assert cat == "other"


def test_get_logs_summary():
    entries = parse_log_text(SAMPLE_LOGS)
    summary = get_logs_summary(entries)

    assert summary["total"] == 6
    assert summary["warnings_and_errors"] == 3  # 2 WARNINGs + 1 ERROR
    assert summary["level_counts"]["INFO"] == 3
    assert summary["level_counts"]["WARNING"] == 2
    assert summary["level_counts"]["ERROR"] == 1
    assert summary["category_counts"]["jobs"] == 2
    assert summary["category_counts"]["sources"] == 1
    assert summary["category_counts"]["garmin"] == 1
    assert summary["category_counts"]["http"] == 1
    assert summary["category_counts"]["server"] == 1


def test_filter_entries():
    entries = parse_log_text(SAMPLE_LOGS)

    # Filter by level
    warn_entries = filter_entries(entries, level="WARNING")
    assert len(warn_entries) == 2

    warner_entries = filter_entries(entries, level="WARNER")
    assert len(warner_entries) == 3

    # Filter by category
    garmin_entries = filter_entries(entries, category="garmin")
    assert len(garmin_entries) == 1
    assert "garminconnect" in garmin_entries[0].logger

    # Filter by search term
    search_entries = filter_entries(entries, search="429")
    assert len(search_entries) == 1
    assert "garminconnect" in search_entries[0].logger


def test_purge_logs(settings):
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(SAMPLE_LOGS)

    # Create dummy backup file
    backup_file = log_file.parent / f"{log_file.name}.1"
    backup_file.write_text("old logs")

    assert log_file.exists()
    assert backup_file.exists()

    ok, msg = purge_logs(settings)
    assert ok
    assert "purged" in msg.lower()

    # Verify backup is deleted and active log has new start entry
    assert not backup_file.exists()
    current_content = log_file.read_text()
    assert "All logs were cleared by administrator" in current_content


def test_purge_logs_by_category(settings):
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(SAMPLE_LOGS)

    ok, msg = purge_logs(settings, category="jobs")
    assert ok
    assert "purged" in msg.lower()

    content = log_file.read_text()
    # Jobs lines should be gone
    assert "activity 100" not in content
    # Other categories should be preserved!
    assert "garminconnect" in content
    assert "app.source_scheduler" in content
    assert "Cleared" in content


def test_web_routes_logs_and_purge(settings):
    db = Database(settings.sqlite_path)
    db.init()
    service = BridgeService(settings, db)
    service.setup()

    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_file.write_text(SAMPLE_LOGS)

    app = create_app(settings, start_background=False)
    app.state.db = db
    app.state.service = service
    client = TestClient(app)

    # 1. GET /logs formatted
    resp = client.get("/logs")
    assert resp.status_code == 200
    assert "Logs & System Events" in resp.text
    assert "app.jobs" in resp.text
    assert "Categories" in resp.text

    # 2. GET /logs with category filter
    resp_cat = client.get("/logs?category=garmin")
    assert resp_cat.status_code == 200
    assert "garminconnect" in resp_cat.text

    # 3. GET /logs/download
    resp_dl = client.get("/logs/download")
    assert resp_dl.status_code == 200
    assert "attachment" in resp_dl.headers.get("content-disposition", "")
    assert "Removed repeated source file" in resp_dl.text

    # 4. POST /logs/purge category
    resp_purge_cat = client.post(
        "/logs/purge",
        data={"category": "http", "csrf_token": ""},
        follow_redirects=True,
    )
    assert resp_purge_cat.status_code == 200
    assert "HTTP & Updates" in resp_purge_cat.text

    # 5. POST /logs/purge all
    resp_purge = client.post(
        "/logs/purge",
        data={"category": "all", "csrf_token": ""},
        follow_redirects=True,
    )
    assert resp_purge.status_code == 200
    assert "purged" in resp_purge.text
    assert "All logs were cleared by administrator" in resp_purge.text
