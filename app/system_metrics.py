from __future__ import annotations

import os
import platform
import resource
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db import Database
from app.fit_preview import build_activity_preview
from app.settings import Settings


@dataclass(frozen=True)
class SystemStatus:
    platform_name: str
    architecture: str
    python_version: str
    cpu_cores: int
    load_averages: list[float]
    process_rss_mb: float
    process_peak_rss_mb: float
    total_ram_mb: float
    available_ram_mb: float
    ram_usage_pct: float
    appdata_free_gb: float
    data_free_gb: float
    db_size_bytes: int
    db_wal_size_bytes: int
    sqlite_journal_mode: str
    sqlite_synchronous: str
    sqlite_temp_store: str
    sqlite_busy_timeout: int
    hardware_tier: str


def get_system_status(settings: Settings, db: Database) -> SystemStatus:
    # CPU & System load
    load_avg = [round(x, 2) for x in (os.getloadavg() if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0])]
    cores = os.cpu_count() or 1

    # RAM (process and host)
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    max_rss_bytes = rusage.ru_maxrss * (1024 if platform.system() != "Darwin" else 1)
    process_peak_rss_mb = round(max_rss_bytes / (1024 * 1024), 2)

    process_rss_mb = 0.0
    if os.path.exists("/proc/self/statm"):
        try:
            with open("/proc/self/statm") as f:
                pages = int(f.read().split()[1])
                process_rss_mb = round((pages * os.sysconf("SC_PAGE_SIZE")) / (1024 * 1024), 2)
        except Exception:
            process_rss_mb = process_peak_rss_mb
    else:
        process_rss_mb = process_peak_rss_mb

    total_ram_mb, available_ram_mb = _get_ram_info()
    ram_usage_pct = (
        round(((total_ram_mb - available_ram_mb) / total_ram_mb) * 100, 1)
        if total_ram_mb > 0
        else 0.0
    )

    # Disk free space
    appdata_free = _get_disk_free_gb(settings.runtime_config_path.parent)
    data_free = _get_disk_free_gb(settings.incoming_dir)

    # SQLite DB metadata
    db_size = settings.sqlite_path.stat().st_size if settings.sqlite_path.exists() else 0
    wal_path = Path(str(settings.sqlite_path) + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    journal_mode, synchronous, temp_store, busy_timeout = _get_sqlite_pragmas(db)
    tier = _classify_hardware(total_ram_mb, cores)

    return SystemStatus(
        platform_name=f"{platform.system()} {platform.release()}",
        architecture=platform.machine(),
        python_version=platform.python_version(),
        cpu_cores=cores,
        load_averages=load_avg,
        process_rss_mb=process_rss_mb,
        process_peak_rss_mb=process_peak_rss_mb,
        total_ram_mb=total_ram_mb,
        available_ram_mb=available_ram_mb,
        ram_usage_pct=ram_usage_pct,
        appdata_free_gb=appdata_free,
        data_free_gb=data_free,
        db_size_bytes=db_size,
        db_wal_size_bytes=wal_size,
        sqlite_journal_mode=journal_mode,
        sqlite_synchronous=synchronous,
        sqlite_temp_store=temp_store,
        sqlite_busy_timeout=busy_timeout,
        hardware_tier=tier,
    )


def run_system_benchmark(settings: Settings, db: Database) -> dict[str, Any]:
    start_time = time.perf_counter()
    rusage_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # Test 1: SQLite Write Latency Benchmark (100 updates)
    db_benchmark = _benchmark_sqlite_writes(db)

    # Test 2: Disk I/O Throughput Benchmark
    disk_benchmark = _benchmark_disk_io(settings.runtime_config_path.parent)

    # Test 3: FIT Parsing & Preview Generation Benchmark
    fit_benchmark = _benchmark_fit_parsing(db, settings)

    rusage_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_delta_mb = round(
        ((rusage_after - rusage_before) * (1024 if platform.system() != "Darwin" else 1)) / (1024 * 1024),
        2,
    )
    total_benchmark_duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_duration_ms": total_benchmark_duration_ms,
        "rss_delta_mb": rss_delta_mb,
        "db_writes": db_benchmark,
        "disk_io": disk_benchmark,
        "fit_parsing": fit_benchmark,
    }


def _get_ram_info() -> tuple[float, float]:
    total_mb = 0.0
    available_mb = 0.0
    if os.path.exists("/proc/meminfo"):
        try:
            mem: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].split()[0].strip()
                        mem[key] = int(val)
            total_mb = round(mem.get("MemTotal", 0) / 1024, 1)
            available_mb = round(mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024, 1)
        except Exception:
            pass
    return total_mb, available_mb


def _get_disk_free_gb(path: Path) -> float:
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
        return round(usage.free / (1024 * 1024 * 1024), 2)
    except Exception:
        return 0.0


def _get_sqlite_pragmas(db: Database) -> tuple[str, str, str, int]:
    try:
        with db.connect() as conn:
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            sync_val = conn.execute("PRAGMA synchronous").fetchone()[0]
            sync_names = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
            synchronous = sync_names.get(int(sync_val), str(sync_val))
            temp_store_val = conn.execute("PRAGMA temp_store").fetchone()[0]
            temp_store_names = {0: "DEFAULT", 1: "FILE", 2: "MEMORY"}
            temp_store = temp_store_names.get(int(temp_store_val), str(temp_store_val))
            busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            return journal_mode, synchronous, temp_store, busy_timeout
    except Exception:
        return "UNKNOWN", "UNKNOWN", "UNKNOWN", 0


def _classify_hardware(ram_mb: float, cores: int) -> str:
    if ram_mb > 0 and ram_mb <= 1200:
        return "Low-spec (Raspberry Pi 3 / Pi Zero 2W)"
    elif ram_mb <= 4096:
        return "Mid-spec (Raspberry Pi 4)"
    else:
        return f"High-spec (Raspberry Pi 5 / Server with {cores} CPU cores)"


def _benchmark_sqlite_writes(db: Database) -> dict[str, Any]:
    iterations = 100
    start = time.perf_counter()
    try:
        with db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _benchmark_test (id INT PRIMARY KEY, val TEXT, ts TEXT)"
            )
            conn.execute("DELETE FROM _benchmark_test")
            conn.commit()

            for i in range(iterations):
                conn.execute(
                    "INSERT INTO _benchmark_test (id, val, ts) VALUES (?, ?, ?)",
                    (i, f"test_data_{i}", time.time()),
                )
                conn.commit()

            conn.execute("DROP TABLE IF EXISTS _benchmark_test")
            conn.commit()
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        avg_latency_ms = round(duration_ms / iterations, 2)
        ops_per_sec = round((iterations / (duration_ms / 1000.0)), 1)
        return {
            "iterations": iterations,
            "total_ms": duration_ms,
            "avg_latency_ms": avg_latency_ms,
            "ops_per_sec": ops_per_sec,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _benchmark_disk_io(target_dir: Path) -> dict[str, Any]:
    file_path = target_dir / "_io_benchmark.tmp"
    data = b"0" * (5 * 1024 * 1024)  # 5 MB payload
    try:
        # Write test
        w_start = time.perf_counter()
        file_path.write_bytes(data)
        os.sync() if hasattr(os, "sync") else None
        w_duration = time.perf_counter() - w_start
        write_mbps = round(5.0 / w_duration, 2) if w_duration > 0 else 0.0

        # Read test
        r_start = time.perf_counter()
        _ = file_path.read_bytes()
        r_duration = time.perf_counter() - r_start
        read_mbps = round(5.0 / r_duration, 2) if r_duration > 0 else 0.0

        file_path.unlink(missing_ok=True)
        return {
            "payload_mb": 5.0,
            "write_mbps": write_mbps,
            "read_mbps": read_mbps,
        }
    except Exception as exc:
        file_path.unlink(missing_ok=True)
        return {"error": str(exc)}


def _benchmark_fit_parsing(db: Database, settings: Settings) -> dict[str, Any]:
    activities = db.list_recent(10)
    valid_activity = None
    for act in activities:
        path_str = act.get("current_path") or act.get("source_path")
        if path_str and Path(str(path_str)).is_file():
            valid_activity = act
            break

    if valid_activity is None:
        for directory in (settings.incoming_dir, settings.uploaded_dir):
            for fit_file in directory.glob("*.fit"):
                if fit_file.is_file():
                    valid_activity = {
                        "id": 0,
                        "current_path": str(fit_file),
                        "filename": fit_file.name,
                    }
                    break
            if valid_activity:
                break

    if valid_activity is None:
        return {"message": "No sample .fit activity file was found to run parsing test."}

    start = time.perf_counter()
    preview = build_activity_preview(valid_activity)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    return {
        "filename": str(valid_activity.get("filename")),
        "parsing_duration_ms": duration_ms,
        "preview_available": preview.available,
        "summary": preview.summary,
        "svg_bytes": len(preview.route_svg) if preview.route_svg else 0,
    }
