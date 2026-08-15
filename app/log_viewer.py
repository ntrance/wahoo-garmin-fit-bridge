from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.settings import Settings

LOG_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+([A-Z]+)\s+\[([^\]]+)\]\s+(.*)$"
)

CATEGORY_MAP = {
    "jobs": ("Sync & Processing", "🔄"),
    "sources": ("Sources & Scheduler", "⏱️"),
    "garmin": ("Garmin Connect", "⌚"),
    "http": ("HTTP & Updates", "🌐"),
    "server": ("Web Server", "🖥️"),
    "other": ("System & Other", "⚙️"),
}


@dataclass
class LogEntry:
    timestamp: str
    level: str
    logger: str
    category: str
    category_label: str
    category_icon: str
    message: str
    raw: str
    line_number: int


def categorize_logger(logger: str, message: str) -> tuple[str, str, str]:
    """Map logger name and message to a user-friendly category."""
    norm = logger.lower()
    msg_norm = message.lower()

    if "app.jobs" in norm or "job" in norm or "rewrite" in msg_norm:
        return ("jobs", CATEGORY_MAP["jobs"][0], CATEGORY_MAP["jobs"][1])
    if "source" in norm or "scheduler" in norm or "dropbox" in norm or "igpsport" in norm or "coros" in norm:
        return ("sources", CATEGORY_MAP["sources"][0], CATEGORY_MAP["sources"][1])
    if "garmin" in norm or "garminconnect" in norm:
        return ("garmin", CATEGORY_MAP["garmin"][0], CATEGORY_MAP["garmin"][1])
    if "httpx" in norm or "update" in norm or "release" in msg_norm:
        return ("http", CATEGORY_MAP["http"][0], CATEGORY_MAP["http"][1])
    if "uvicorn" in norm or "fastapi" in norm or "starlette" in norm:
        return ("server", CATEGORY_MAP["server"][0], CATEGORY_MAP["server"][1])
    return ("other", CATEGORY_MAP["other"][0], CATEGORY_MAP["other"][1])


def parse_log_text(text: str, max_lines: int = 1500) -> list[LogEntry]:
    """Parse raw log file content into structured LogEntry objects."""
    if not text.strip():
        return []

    lines = text.splitlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]

    entries: list[LogEntry] = []
    current_entry: LogEntry | None = None

    for idx, line in enumerate(lines, start=1):
        match = LOG_PATTERN.match(line)
        if match:
            timestamp, level, logger_name, msg = match.groups()
            cat_id, cat_label, cat_icon = categorize_logger(logger_name, msg)
            current_entry = LogEntry(
                timestamp=timestamp,
                level=level.upper(),
                logger=logger_name,
                category=cat_id,
                category_label=cat_label,
                category_icon=cat_icon,
                message=msg,
                raw=line,
                line_number=idx,
            )
            entries.append(current_entry)
        elif current_entry is not None:
            # Multi-line log (traceback, json, indented text)
            current_entry.message += "\n" + line
            current_entry.raw += "\n" + line
        else:
            # Unrecognized single line
            entries.append(
                LogEntry(
                    timestamp="",
                    level="INFO",
                    logger="system",
                    category="other",
                    category_label=CATEGORY_MAP["other"][0],
                    category_icon=CATEGORY_MAP["other"][1],
                    message=line,
                    raw=line,
                    line_number=idx,
                )
            )

    return entries


def parse_log_file(path: Path, max_lines: int = 1500) -> list[LogEntry]:
    """Read and parse the application log file."""
    if not path.exists():
        return []
    try:
        text = path.read_text(errors="replace")
        return parse_log_text(text, max_lines=max_lines)
    except OSError:
        return []


def get_logs_summary(entries: list[LogEntry]) -> dict[str, Any]:
    """Generate counts and stats for category and level filters."""
    level_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0, "DEBUG": 0, "OTHER": 0}
    category_counts: dict[str, int] = {cat: 0 for cat in CATEGORY_MAP}
    warnings_and_errors = 0

    for entry in entries:
        lvl = entry.level if entry.level in level_counts else "OTHER"
        level_counts[lvl] += 1
        if entry.level in {"WARNING", "ERROR", "CRITICAL"}:
            warnings_and_errors += 1

        cat = entry.category if entry.category in category_counts else "other"
        category_counts[cat] += 1

    categories = [
        {
            "id": cat_id,
            "label": label,
            "icon": icon,
            "count": category_counts.get(cat_id, 0),
        }
        for cat_id, (label, icon) in CATEGORY_MAP.items()
    ]

    return {
        "total": len(entries),
        "warnings_and_errors": warnings_and_errors,
        "level_counts": level_counts,
        "category_counts": category_counts,
        "categories": categories,
    }


def filter_entries(
    entries: list[LogEntry],
    level: str = "all",
    category: str = "all",
    search: str = "",
) -> list[LogEntry]:
    """Filter entries based on level, category, and search text."""
    filtered = entries
    if level and level.lower() != "all":
        target_level = level.upper()
        if target_level == "WARNER":
            filtered = [e for e in filtered if e.level in {"WARNING", "ERROR", "CRITICAL"}]
        else:
            filtered = [e for e in filtered if e.level == target_level]

    if category and category.lower() != "all":
        target_cat = category.lower()
        filtered = [e for e in filtered if e.category == target_cat]

    if search and search.strip():
        q = search.strip().lower()
        filtered = [
            e
            for e in filtered
            if q in e.message.lower()
            or q in e.logger.lower()
            or q in e.timestamp.lower()
            or q in e.category_label.lower()
        ]

    return filtered


def purge_logs(settings: Settings) -> tuple[bool, str]:
    """Clear the active log file and any rotated backups."""
    purged_count = 0
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        # 1. Truncate active log file
        log_file = settings.log_file
        if log_file.exists():
            log_file.write_text("")
            purged_count += 1

        # 2. Remove rotated log backup files (app.log.1, app.log.2, etc.)
        for backup_file in settings.log_dir.glob(f"{log_file.name}.*"):
            try:
                backup_file.unlink(missing_ok=True)
                purged_count += 1
            except OSError:
                pass

        # 3. Write a fresh clean start notice
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        fresh_line = f"{now_iso} INFO [app.log_viewer] Logs were cleared by administrator.\n"
        with log_file.open("a") as handle:
            handle.write(fresh_line)

        return True, f"Logs successfully purged (cleared active log and {purged_count - 1} backup files)."
    except Exception as exc:
        return False, f"Failed to purge logs: {exc}"
