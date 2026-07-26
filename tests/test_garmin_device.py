from __future__ import annotations

from app.garmin_device import GarminDevice, dedupe_devices, garmin_device_presets, garmin_product_display_name


def test_dedupe_devices_prefers_device_with_software_version():
    plain = GarminDevice(
        id="1:3291:123",
        label="plain",
        manufacturer_id=1,
        product_id=3291,
        unit_id=123,
        software_version=None,
        software_version_label="",
        garmin_product="fenix6x",
        source_file="a.fit",
    )
    detailed = GarminDevice(
        id="1:3291:123",
        label="detailed",
        manufacturer_id=1,
        product_id=3291,
        unit_id=123,
        software_version=2802,
        software_version_label="28.02",
        garmin_product="fenix6x",
        source_file="b.fit",
    )

    assert dedupe_devices([plain, detailed]) == [detailed]


def test_fenix_product_id_has_friendly_name():
    assert garmin_product_display_name("fenix6x", 3291) == "Garmin Fenix 6X Pro"


def test_no_shared_device_identity_is_published():
    assert garmin_device_presets() == []
