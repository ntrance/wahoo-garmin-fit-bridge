from __future__ import annotations

from app.db import Database
from app.settings import Settings
from app.setup_status import (
    delete_dropbox_source,
    sync_dropbox_to_incoming,
    test_dropbox,
)
from app.sources.base import (
    SourceFileMetadata,
    SourceResult,
    SourceSyncResult,
    write_source_sidecar,
)


class DropboxSource:
    source_type = "dropbox"
    display_name = "Wahoo/ELEMNT via Dropbox"

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.poll_seconds = settings.dropbox_poll_seconds

    def is_enabled(self) -> bool:
        return self.settings.dropbox_source_enabled

    def is_configured(self) -> bool:
        if not self.settings.rclone_config_path.is_file():
            return False
        marker = f"[{self.settings.rclone_remote}]"
        try:
            return marker in self.settings.rclone_config_path.read_text(encoding="utf-8")
        except OSError:
            return False

    def test_connection(self) -> SourceResult:
        result = test_dropbox(self.settings)
        return SourceResult(result.ok, result.title, result.output)

    def sync_to_incoming(self, *, historical: bool = False) -> SourceSyncResult:
        before = {
            path.name
            for path in self.settings.incoming_dir.glob("*.fit")
            if path.is_file()
        }
        result = sync_dropbox_to_incoming(self.settings)
        after = {
            path.name
            for path in self.settings.incoming_dir.glob("*.fit")
            if path.is_file()
        }
        downloaded = sorted(after - before)
        remote_root = self.settings.dropbox_wahoo_path.strip("/")
        for filename in downloaded:
            remote_path = f"{remote_root}/{filename}" if remote_root else filename
            write_source_sidecar(
                self.settings.incoming_dir / filename,
                SourceFileMetadata(
                    source_type=self.source_type,
                    source_external_id=remote_path,
                    source_display_name="Dropbox",
                    source_original_filename=filename,
                    source_remote_path=remote_path,
                ),
            )
        return SourceSyncResult(
            ok=result.ok,
            title=result.title,
            message=result.output,
            downloaded=len(downloaded),
        )

    def supports_remote_delete(self) -> bool:
        return True

    def delete_remote_activity(self, external_id: str) -> SourceResult:
        filename = external_id.rsplit("/", 1)[-1]
        result = delete_dropbox_source(self.settings, filename)
        return SourceResult(result.ok, result.title, result.output)
