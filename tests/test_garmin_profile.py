import json

from app.garmin_profile import (
    GarminProfile,
    garmin_profile_path,
    garmin_token_dir,
    has_garmin_token_files,
    load_garmin_profile,
    save_garmin_profile,
)


def test_profile_round_trip_uses_private_native_config(settings):
    profile = GarminProfile("wahoo", "rider@example.com", "secret", 1, 3291, 12345, 710)

    path = save_garmin_profile(settings, profile)

    assert path == garmin_profile_path(settings)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_garmin_profile(settings) == profile
    assert garmin_token_dir(settings) == settings.garmin_config_dir / "tokens"


def test_migrates_legacy_profile_and_tokens(settings):
    legacy_root = settings.garmin_config_dir.parent / "fit-file-faker"
    legacy_config = legacy_root / "config" / "FitFileFaker" / ".config.json"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text(
        json.dumps(
            {
                "default_profile": "wahoo",
                "profiles": [
                    {
                        "name": "wahoo",
                        "garmin_username": "rider@example.com",
                        "garmin_password": "secret",
                        "manufacturer": 1,
                        "device": 3291,
                        "serial_number": 12345,
                        "software_version": 710,
                    }
                ],
            }
        )
    )
    legacy_tokens = legacy_root / "data" / "FitFileFaker" / ".garmin_wahoo"
    legacy_tokens.mkdir(parents=True)
    (legacy_tokens / "oauth1_token.json").write_text("{}")

    profile = load_garmin_profile(settings)

    assert profile is not None
    assert profile.garmin_username == "rider@example.com"
    assert garmin_profile_path(settings).exists()
    assert (garmin_token_dir(settings) / "oauth1_token.json").exists()


def test_has_garmin_token_files_ignores_hidden_files(tmp_path):
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / ".keep").write_text("")

    assert not has_garmin_token_files(token_dir)

    (token_dir / "oauth1_token.json").write_text("{}")

    assert has_garmin_token_files(token_dir)
