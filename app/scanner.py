from __future__ import annotations

import argparse
import time

from app.db import Database
from app.jobs import BridgeService
from app.logging_config import configure_logging
from app.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan and process Wahoo FIT files")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings)
    service = BridgeService(settings, Database(settings.sqlite_path))
    service.setup()

    if args.once:
        service.scan_once()
        return

    while True:
        service.scan_once()
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()

