from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler

from app.settings import Settings
from app.redaction import redact_sensitive_text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage(), tuple(self._secrets))
        record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    redactor = RedactingFilter([settings.web_password, settings.garmin_unit_id])

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(redactor)

    root.addHandler(stream)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_file,
            maxBytes=2_000_000,
            backupCount=3,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    except OSError:
        root.warning("File logging unavailable at %s; using console logging only", settings.log_file)
