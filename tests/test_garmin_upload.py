from __future__ import annotations

from datetime import UTC, datetime, timedelta

from garmin_fit_sdk import Decoder, Encoder, Stream

from app.garmin_profile import GarminProfile
from app.garmin_upload import (
    _rewrite_wahoo_fit,
    friendly_upload_error,
    looks_like_duplicate,
    run_garmin_upload,
)
from app.garmin_guard import (
    active_garmin_cooldown,
    clear_garmin_cooldown,
    detects_garmin_rate_limit,
    garmin_guard_path,
    record_garmin_rate_limit,
)


def test_duplicate_output_detection():
    assert looks_like_duplicate("Received HTTP conflict (activity already exists)")
    assert looks_like_duplicate("duplicate upload")
    assert not looks_like_duplicate("Successfully uploaded ride.fit")


def test_garmin_rate_limit_detection(settings):
    output = "Mobile login returned 429 - IP rate limited by Garmin"
    cloudflare_output = "Mobile login: HTTP 403 (Cloudflare bot challenge)"

    assert detects_garmin_rate_limit(output)
    assert detects_garmin_rate_limit(cloudflare_output)
    assert "blocking or rate-limiting" in friendly_upload_error(output, 1)

    record_garmin_rate_limit(settings, output)
    cooldown = active_garmin_cooldown(settings)

    assert cooldown is not None
    assert "paused Garmin login/upload attempts" in cooldown["message"]
    assert garmin_guard_path(settings).stat().st_mode & 0o777 == 0o600


def test_garmin_upload_skips_when_garmin_is_rate_limited(settings):
    record_garmin_rate_limit(settings, "Mobile login returned 429 - IP rate limited by Garmin")
    fit_path = settings.incoming_dir / "ride.fit"
    fit_path.parent.mkdir(parents=True, exist_ok=True)
    fit_path.write_bytes(b"fit")

    result = run_garmin_upload(fit_path, settings)

    assert not result.success
    assert result.return_code == 75
    assert "paused Garmin login/upload attempts" in result.combined_output


def test_garmin_cooldown_can_be_cleared(settings):
    record_garmin_rate_limit(settings, "Mobile login returned 429 - IP rate limited by Garmin")

    assert active_garmin_cooldown(settings) is not None
    assert clear_garmin_cooldown(settings)
    assert active_garmin_cooldown(settings) is None


def test_native_converter_preserves_records_and_applies_device_identity(tmp_path):
    source = tmp_path / "wahoo.fit"
    started_at = datetime(2026, 7, 19, 5, 22, 24, tzinfo=UTC)
    encoder = Encoder()
    encoder.on_mesg(
        0,
        {
            "type": "activity",
            "manufacturer": 32,
            "product": 1,
            "serial_number": 999,
            "time_created": started_at,
        },
    )
    for index in range(3):
        encoder.on_mesg(
            20,
            {
                "timestamp": started_at + timedelta(seconds=index),
                "distance": float(index * 10),
                "speed": 5.0,
            },
        )
    source.write_bytes(encoder.close())
    original = source.read_bytes()
    profile = GarminProfile("wahoo", "rider@example.com", "secret", 1, 3291, 12345, 710)

    success, note, record_count = _rewrite_wahoo_fit(source, profile, "Garmin Device")

    with source.open("rb") as handle:
        decoder = Decoder(Stream(handle, source.stat().st_size))
        assert decoder.check_integrity()
    assert success
    assert "Garmin FIT SDK rewrote" in note
    assert "Garmin Device" in note
    assert source.read_bytes() != original
    assert record_count == 3
