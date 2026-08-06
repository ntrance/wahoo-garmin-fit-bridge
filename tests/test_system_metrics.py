from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.settings import Settings
from app.system_metrics import get_system_status, run_system_benchmark


def test_get_system_status(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.init()
    settings = Settings.from_env()

    status = get_system_status(settings, db)
    assert status.cpu_cores >= 1
    assert status.hardware_tier != ""
    assert status.sqlite_journal_mode.upper() == "WAL"


def test_run_system_benchmark(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.init()
    settings = Settings.from_env()

    result = run_system_benchmark(settings, db)
    assert "total_duration_ms" in result
    assert "db_writes" in result
    assert result["db_writes"]["iterations"] == 100
    assert result["db_writes"]["ops_per_sec"] > 0
    assert "disk_io" in result
    assert "fit_parsing" in result


def test_system_endpoints(settings: Settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        # Test GET /system
        res_system = client.get("/system")
        assert res_system.status_code == 200
        assert "System Test" in res_system.text
        assert "Hardware Classifier" in res_system.text

        # Test POST /api/benchmark
        res_bench = client.post("/api/benchmark")
        assert res_bench.status_code == 200
        data = res_bench.json()
        assert "total_duration_ms" in data
        assert "db_writes" in data
