from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from app.private_files import write_private_text


@dataclass(frozen=True)
class SourceResult:
    ok: bool
    title: str
    message: str
    requires_attention: bool = False
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class SourceSyncResult(SourceResult):
    downloaded: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class SourceFileMetadata:
    source_type: str
    source_external_id: str | None
    source_display_name: str
    source_original_filename: str | None = None
    source_remote_path: str | None = None
    force_dry_run: bool = False


class ActivitySource(Protocol):
    source_type: str
    display_name: str
    poll_seconds: int

    def is_enabled(self) -> bool: ...

    def is_configured(self) -> bool: ...

    def test_connection(self) -> SourceResult: ...

    def sync_to_incoming(self, *, historical: bool = False) -> SourceSyncResult: ...

    def supports_remote_delete(self) -> bool: ...

    def delete_remote_activity(self, external_id: str) -> SourceResult: ...


def source_sidecar_path(fit_path: Path) -> Path:
    return fit_path.with_name(f"{fit_path.name}.source.json")


def write_source_sidecar(fit_path: Path, metadata: SourceFileMetadata) -> None:
    write_private_text(
        source_sidecar_path(fit_path),
        json.dumps(asdict(metadata), sort_keys=True) + "\n",
    )


def read_source_sidecar(fit_path: Path) -> SourceFileMetadata:
    sidecar = source_sidecar_path(fit_path)
    if not sidecar.is_file():
        return SourceFileMetadata(
            source_type="local",
            source_external_id=None,
            source_display_name="Local / legacy",
            source_original_filename=fit_path.name,
        )
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        return SourceFileMetadata(
            source_type=str(payload.get("source_type") or "local"),
            source_external_id=(
                str(payload["source_external_id"])
                if payload.get("source_external_id") is not None
                else None
            ),
            source_display_name=str(
                payload.get("source_display_name") or "Local / legacy"
            ),
            source_original_filename=(
                str(payload["source_original_filename"])
                if payload.get("source_original_filename")
                else None
            ),
            source_remote_path=(
                str(payload["source_remote_path"])
                if payload.get("source_remote_path")
                else None
            ),
            force_dry_run=bool(payload.get("force_dry_run", False)),
        )
    except (OSError, ValueError, TypeError):
        return SourceFileMetadata(
            source_type="local",
            source_external_id=None,
            source_display_name="Local / legacy",
            source_original_filename=fit_path.name,
        )


def remove_source_sidecar(fit_path: Path) -> None:
    source_sidecar_path(fit_path).unlink(missing_ok=True)
