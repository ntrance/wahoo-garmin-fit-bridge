from __future__ import annotations

from app.fit_metadata import _find_total_distance, compute_fit_metadata
from conftest import write_fit_like


def test_metadata_falls_back_when_fit_decode_fails(tmp_path):
    fit_path = write_fit_like(tmp_path / "ride.fit", b"not a valid fit")

    metadata = compute_fit_metadata(fit_path)

    assert metadata.file_size == len(b"not a valid fit")
    assert len(metadata.sha256) == 64
    assert metadata.activity_start_time is None
    assert metadata.total_distance_meters is None


def test_total_distance_prefers_session_summary():
    messages = {
        "lap_mesgs": [{"total_distance": 4000}],
        "session_mesgs": [{"total_distance": 10358.98}],
        "record_mesgs": [{"distance": 1}, {"distance": 5000}],
    }

    assert _find_total_distance(messages) == 10358.98


def test_total_distance_falls_back_to_last_record_distance():
    messages = {
        "record_mesgs": [{"distance": 0}, {"distance": 92636.11}],
    }

    assert _find_total_distance(messages) == 92636.11
