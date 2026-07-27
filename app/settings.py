from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

IGPSPORT_REGIONS = (
    ("international", "International", "https://prod.en.igpsport.com/service"),
    ("china", "China", "https://prod.zh.igpsport.com/service"),
)
IGPSPORT_DEFAULT_BASE_URL = IGPSPORT_REGIONS[0][2]


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    app_name: str
    poll_seconds: int
    dropbox_source_enabled: bool
    igpsport_source_enabled: bool
    dropbox_poll_seconds: int
    igpsport_poll_seconds: int
    igpsport_min_poll_seconds: int
    igpsport_max_pages_per_poll: int
    igpsport_base_url: str
    igpsport_config_dir: Path
    igpsport_import_mode: str
    max_retries: int
    log_level: str
    web_auth_enabled: bool
    web_username: str
    web_password: str
    web_password_hash: str
    session_secret_key: str
    session_cookie_name: str
    session_cookie_secure: bool
    session_max_age_seconds: int
    login_rate_limit_attempts: int
    login_rate_limit_window_seconds: int
    dry_run: bool
    rclone_remote: str
    dropbox_wahoo_path: str
    rclone_config_path: Path
    incoming_dir: Path
    processing_dir: Path
    uploaded_dir: Path
    duplicate_dir: Path
    failed_dir: Path
    archive_dir: Path
    sqlite_path: Path
    log_dir: Path
    runtime_config_path: Path
    real_fit_dir: Path
    real_fit_upload_dir: Path
    max_real_fit_upload_bytes: int
    detected_devices_path: Path
    garmin_config_dir: Path
    garmin_profile_name: str
    garmin_device_name: str
    garmin_unit_id: str

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        if env_file is not None:
            load_dotenv(env_file)
        else:
            load_dotenv()
        runtime_config_path = Path(os.getenv("RUNTIME_CONFIG_PATH", "/appdata/runtime.env"))
        if runtime_config_path.exists():
            load_dotenv(runtime_config_path, override=True)

        poll_seconds = _int_env("POLL_SECONDS", 60)
        igpsport_min_poll_seconds = max(_int_env("IGPSPORT_MIN_POLL_SECONDS", 300), 300)
        return cls(
            app_name=os.getenv("APP_NAME", "fit-to-garmin-bridge"),
            poll_seconds=poll_seconds,
            dropbox_source_enabled=_bool_env("DROPBOX_SOURCE_ENABLED", True),
            igpsport_source_enabled=_bool_env("IGPSPORT_SOURCE_ENABLED", False),
            dropbox_poll_seconds=max(_int_env("DROPBOX_POLL_SECONDS", poll_seconds), 10),
            igpsport_poll_seconds=max(
                _int_env("IGPSPORT_POLL_SECONDS", 900),
                igpsport_min_poll_seconds,
            ),
            igpsport_min_poll_seconds=igpsport_min_poll_seconds,
            igpsport_max_pages_per_poll=max(
                1,
                min(_int_env("IGPSPORT_MAX_PAGES_PER_POLL", 3), 20),
            ),
            igpsport_base_url=os.getenv(
                "IGPSPORT_BASE_URL",
                IGPSPORT_DEFAULT_BASE_URL,
            ).rstrip("/"),
            igpsport_config_dir=Path(
                os.getenv("IGPSPORT_CONFIG_DIR", "/appdata/igpsport")
            ),
            igpsport_import_mode=os.getenv(
                "IGPSPORT_IMPORT_MODE",
                "new_only",
            ).strip().lower(),
            max_retries=_int_env("MAX_RETRIES", 3),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            web_auth_enabled=_bool_env("WEB_AUTH_ENABLED", True),
            web_username=os.getenv("WEB_USERNAME", "admin"),
            web_password=os.getenv("WEB_PASSWORD", "change-this-password"),
            web_password_hash=os.getenv("WEB_PASSWORD_HASH", ""),
            session_secret_key=os.getenv(
                "SESSION_SECRET_KEY", "change-this-long-random-secret"
            ),
            session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "wahoo_bridge_session"),
            session_cookie_secure=_bool_env("SESSION_COOKIE_SECURE", False),
            session_max_age_seconds=_int_env("SESSION_MAX_AGE_SECONDS", 43_200),
            login_rate_limit_attempts=_int_env("LOGIN_RATE_LIMIT_ATTEMPTS", 5),
            login_rate_limit_window_seconds=_int_env("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300),
            dry_run=_bool_env("DRY_RUN", True),
            rclone_remote=os.getenv("RCLONE_REMOTE", "dropbox"),
            dropbox_wahoo_path=os.getenv("DROPBOX_WAHOO_PATH", "Apps/WahooFitness"),
            rclone_config_path=Path(os.getenv("RCLONE_CONFIG_PATH", "/appdata/rclone/rclone.conf")),
            incoming_dir=Path(os.getenv("FIT_INCOMING_DIR", "/data/incoming")),
            processing_dir=Path(os.getenv("FIT_PROCESSING_DIR", "/data/processing")),
            uploaded_dir=Path(os.getenv("FIT_UPLOADED_DIR", "/data/uploaded")),
            duplicate_dir=Path(os.getenv("FIT_DUPLICATE_DIR", "/data/duplicate")),
            failed_dir=Path(os.getenv("FIT_FAILED_DIR", "/data/failed")),
            archive_dir=Path(os.getenv("FIT_ARCHIVE_DIR", "/data/archive")),
            sqlite_path=Path(os.getenv("SQLITE_PATH", "/appdata/bridge.sqlite")),
            log_dir=Path(os.getenv("LOG_DIR", "/appdata/logs")),
            runtime_config_path=Path(os.getenv("RUNTIME_CONFIG_PATH", "/appdata/runtime.env")),
            real_fit_dir=Path(os.getenv("REAL_FIT_DIR", "/real_fit")),
            real_fit_upload_dir=Path(os.getenv("REAL_FIT_UPLOAD_DIR", "/appdata/real_fit")),
            max_real_fit_upload_bytes=_int_env("MAX_REAL_FIT_UPLOAD_BYTES", 33_554_432),
            detected_devices_path=Path(os.getenv("DETECTED_DEVICES_PATH", "/appdata/detected_devices.json")),
            garmin_config_dir=Path(os.getenv("GARMIN_CONFIG_DIR", "/appdata/garmin")),
            garmin_profile_name=os.getenv(
                "GARMIN_PROFILE_NAME",
                os.getenv("FIT_FILE_FAKER_PROFILE", "wahoo"),
            ),
            garmin_device_name=os.getenv("GARMIN_DEVICE_NAME", "Garmin Device"),
            garmin_unit_id=os.getenv("GARMIN_UNIT_ID", ""),
        )

    def validate_security(self) -> None:
        if not self.web_auth_enabled:
            return
        if not self.web_username.strip():
            raise ValueError("WEB_USERNAME must not be empty when web authentication is enabled")
        if not self.web_password_hash and self.web_password in {"", "change-this-password"}:
            raise ValueError(
                "Set WEB_PASSWORD to a strong value or configure WEB_PASSWORD_HASH before starting"
            )
        if self.session_secret_key in {"", "change-this-password", "change-this-long-random-secret"}:
            raise ValueError("Set SESSION_SECRET_KEY to a separate long random value before starting")
        if self.web_password and self.session_secret_key == self.web_password:
            raise ValueError("SESSION_SECRET_KEY must be different from WEB_PASSWORD")

    def ensure_directories(self) -> None:
        for directory in (
            self.incoming_dir,
            self.processing_dir,
            self.uploaded_dir,
            self.duplicate_dir,
            self.failed_dir,
            self.archive_dir,
            self.sqlite_path.parent,
            self.log_dir,
            self.runtime_config_path.parent,
            self.real_fit_upload_dir,
            self.detected_devices_path.parent,
            self.garmin_config_dir,
            self.igpsport_config_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def log_file(self) -> Path:
        return self.log_dir / "app.log"
