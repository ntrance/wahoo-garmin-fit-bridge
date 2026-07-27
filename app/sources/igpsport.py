from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.db import Database
from app.private_files import write_private_text
from app.redaction import redact_sensitive_text
from app.settings import Settings
from app.sources.base import (
    SourceFileMetadata,
    SourceResult,
    SourceSyncResult,
    write_source_sidecar,
)

logger = logging.getLogger(__name__)

LOGIN_PATH = "auth/account/login"
ACTIVITY_LIST_PATH = "web-gateway/web-analyze/activity/queryMyActivity"
DOWNLOAD_URL_PATH = "web-gateway/web-analyze/activity/getDownloadUrl/{ride_id}"
USER_AGENT = "fit-to-garmin-bridge/0.1 (+https://github.com/ntrance/wahoo-garmin-fit-bridge)"


class IGPSportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        requires_attention: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.requires_attention = requires_attention
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class IGPSportActivity:
    ride_id: str
    recorded_at: str | None
    original_filename: str | None


class IGPSportStore:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.profile_path = config_dir / "profile.json"
        self.session_path = config_dir / "session.json"
        self.state_path = config_dir / "state.json"

    def load_profile(self) -> dict[str, Any]:
        return self._load_json(self.profile_path)

    def save_profile(
        self,
        *,
        username: str,
        password: str,
        base_url: str,
        import_mode: str,
        cutoff_date: str = "",
    ) -> None:
        existing = self.load_profile()
        saved_password = password or str(existing.get("password") or "")
        normalized_username = username.strip()
        normalized_base_url = base_url.rstrip("/")
        if not normalized_username:
            raise ValueError("iGPSPORT account identifier is required.")
        if not saved_password:
            raise ValueError("iGPSPORT password is required.")
        if urlparse(normalized_base_url).scheme != "https":
            raise ValueError("iGPSPORT base URL must use HTTPS.")
        if import_mode not in {"new_only", "since_date"}:
            raise ValueError("Unsupported iGPSPORT import mode.")
        credentials_changed = (
            str(existing.get("username") or "") != normalized_username
            or str(existing.get("base_url") or "") != normalized_base_url
            or bool(password and password != str(existing.get("password") or ""))
        )
        write_private_text(
            self.profile_path,
            json.dumps(
                {
                    "username": normalized_username,
                    "password": saved_password,
                    "base_url": normalized_base_url,
                    "import_mode": import_mode,
                    "cutoff_date": cutoff_date,
                },
                sort_keys=True,
            )
            + "\n",
        )
        if credentials_changed:
            self.clear_session()

    def delete_profile(self) -> None:
        self.clear_session()
        self.profile_path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)

    def load_session(self) -> dict[str, Any]:
        return self._load_json(self.session_path)

    def save_session(self, token: str, expires_at: str | None = None) -> None:
        write_private_text(
            self.session_path,
            json.dumps(
                {
                    "access_token": token,
                    "expires_at": expires_at,
                    "created_at": _utc_now(),
                    "last_validated_at": _utc_now(),
                },
                sort_keys=True,
            )
            + "\n",
        )

    def clear_session(self) -> None:
        self.session_path.unlink(missing_ok=True)

    def load_state(self) -> dict[str, Any]:
        return self._load_json(self.state_path)

    def save_state(self, values: dict[str, Any]) -> None:
        state = self.load_state()
        state.update(values)
        write_private_text(self.state_path, json.dumps(state, sort_keys=True) + "\n")

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


class IGPSportClient:
    def __init__(
        self,
        store: IGPSportStore,
        *,
        base_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=True,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def login(self) -> str:
        profile = self.store.load_profile()
        username = str(profile.get("username") or "")
        password = str(profile.get("password") or "")
        if not username or not password:
            raise IGPSportError(
                "iGPSPORT profile is incomplete.",
                requires_attention=True,
            )
        response = self._request(
            "POST",
            LOGIN_PATH,
            allow_auth_failure=True,
            json={
                "appId": "igpsport-web",
                "username": username,
                "password": password,
            },
        )
        if response.status_code in {401, 403}:
            raise IGPSportError(
                _login_rejection_message(response),
                requires_attention=True,
            )
        payload = self._json(response, "iGPSPORT login returned invalid JSON.")
        token = _find_string(payload, ("accessToken", "access_token", "token"))
        if not token:
            raise IGPSportError(
                "iGPSPORT login succeeded but returned no access token.",
                requires_attention=True,
            )
        expires_at = _find_string(payload, ("expiresAt", "expires_at", "expiration"))
        self.store.save_session(token, expires_at)
        return token

    def token(self) -> str:
        session = self.store.load_session()
        token = str(session.get("access_token") or "")
        if token and not _is_expired(str(session.get("expires_at") or "")):
            return token
        return self.login()

    def list_activities(self, page: int = 1, page_size: int = 20) -> list[IGPSportActivity]:
        response = self._authenticated_request(
            "GET",
            ACTIVITY_LIST_PATH,
            params={
                "pageNo": page,
                "pageSize": page_size,
                "sort": 1,
                "reqType": 0,
                "format": "fit",
            },
        )
        payload = self._json(response, "iGPSPORT activity list returned invalid JSON.")
        rows = _find_list(payload, ("rows", "records", "list", "activities"))
        activities: list[IGPSportActivity] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ride_id = _find_string(row, ("rideId", "activityId", "id"))
            if not ride_id:
                continue
            activities.append(
                IGPSportActivity(
                    ride_id=ride_id,
                    recorded_at=_find_string(
                        row,
                        (
                            "startTime",
                            "start_time",
                            "rideTime",
                            "activityTime",
                            "createTime",
                        ),
                    ),
                    original_filename=_find_string(
                        row,
                        ("fileName", "filename", "name"),
                    ),
                )
            )
        return activities

    def get_download_url(self, ride_id: str) -> str:
        response = self._authenticated_request(
            "GET",
            DOWNLOAD_URL_PATH.format(ride_id=ride_id),
        )
        payload = self._json(response, "iGPSPORT download response returned invalid JSON.")
        url = _find_string(payload, ("downloadUrl", "download_url", "url"))
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise IGPSportError("iGPSPORT returned an invalid FIT download URL.")
        return url

    def download_fit(self, ride_id: str, incoming_dir: Path, max_bytes: int) -> Path:
        url = self.get_download_url(ride_id)
        incoming_dir.mkdir(parents=True, exist_ok=True)
        final_path = incoming_dir / f"igpsport_{_safe_external_id(ride_id)}.fit"
        temp_path: Path | None = None
        try:
            with self.client.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as response:
                if response.status_code >= 400:
                    raise IGPSportError(
                        f"iGPSPORT FIT download failed with HTTP {response.status_code}."
                    )
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise IGPSportError("iGPSPORT FIT download exceeded the size limit.")
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".igpsport-",
                    suffix=".fit.tmp",
                    dir=incoming_dir,
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    os.chmod(temp_path, 0o600)
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise IGPSportError(
                                "iGPSPORT FIT download exceeded the size limit."
                            )
                        handle.write(chunk)
            if temp_path is None:
                raise IGPSportError("iGPSPORT FIT download did not create a file.")
            validate_fit_file(temp_path)
            os.replace(temp_path, final_path)
            return final_path
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def _authenticated_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = self.token()
        response = self._request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            allow_auth_failure=True,
            **kwargs,
        )
        if response.status_code not in {401, 403}:
            return response
        self.store.clear_session()
        token = self.login()
        return self._request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        allow_auth_failure: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise IGPSportError("Could not connect to iGPSPORT Cloud.") from exc
                time.sleep(0.25 * (attempt + 1))
                continue
            if response.status_code == 429:
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                raise IGPSportError(
                    "iGPSPORT rate limit reached. Automatic polling has been paused.",
                    retry_after_seconds=retry_after,
                )
            if response.status_code in {401, 403} and allow_auth_failure:
                return response
            if response.status_code in {401, 403}:
                raise IGPSportError(
                    "iGPSPORT rejected the saved account or session.",
                    requires_attention=True,
                )
            if response.status_code >= 500 and attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise IGPSportError(
                    f"iGPSPORT request failed with HTTP {response.status_code}."
                )
            return response
        raise IGPSportError(
            f"iGPSPORT request failed with HTTP {response.status_code if response else 500}."
        )

    @staticmethod
    def _json(response: httpx.Response, message: str) -> dict[str, Any] | list[Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise IGPSportError(message) from exc
        if not isinstance(payload, (dict, list)):
            raise IGPSportError(message)
        return payload


class IGPSportSource:
    source_type = "igpsport"
    display_name = "iGPSPORT Cloud"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        client: IGPSportClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.poll_seconds = settings.igpsport_poll_seconds
        self.store = IGPSportStore(settings.igpsport_config_dir)
        self._client_override = client
        self._managed_client: IGPSportClient | None = None
        self._managed_base_url = ""

    def is_enabled(self) -> bool:
        return self.settings.igpsport_source_enabled

    def is_configured(self) -> bool:
        profile = self.store.load_profile()
        return bool(profile.get("username") and profile.get("password"))

    def test_connection(self) -> SourceResult:
        try:
            self._client().list_activities(page=1, page_size=1)
            return SourceResult(True, "iGPSPORT connection", "iGPSPORT login and activity access succeeded.")
        except IGPSportError as exc:
            return self._error_result("iGPSPORT connection", exc)

    def sync_to_incoming(self, *, historical: bool = False) -> SourceSyncResult:
        if historical:
            return SourceSyncResult(
                False,
                "iGPSPORT sync",
                "Historical imports must use the bounded historical import action.",
            )
        if not self.is_configured():
            return SourceSyncResult(
                False,
                "iGPSPORT sync",
                "iGPSPORT profile is not configured.",
                requires_attention=True,
            )
        try:
            return self._sync_incremental()
        except IGPSportError as exc:
            result = self._error_result("iGPSPORT sync", exc)
            return SourceSyncResult(
                ok=result.ok,
                title=result.title,
                message=result.message,
                requires_attention=result.requires_attention,
                retry_after_seconds=result.retry_after_seconds,
            )

    def supports_remote_delete(self) -> bool:
        return False

    def delete_remote_activity(self, external_id: str) -> SourceResult:
        del external_id
        return SourceResult(
            False,
            "iGPSPORT delete",
            "Remote deletion is not supported for iGPSPORT Cloud.",
        )

    def import_history(
        self,
        *,
        start_date: str,
        end_date: str,
        max_activities: int,
        dry_run: bool,
    ) -> SourceSyncResult:
        start = _parse_time(start_date)
        end = _parse_time(end_date) if end_date else None
        if start is None:
            return SourceSyncResult(
                False,
                "iGPSPORT historical import",
                "A valid historical import start date is required.",
            )
        limit = min(max(max_activities, 1), 500)
        selected: list[IGPSportActivity] = []
        skipped = 0
        max_pages = min(25, (limit + 19) // 20 + 2)
        try:
            for page in range(1, max_pages + 1):
                activities = self._client().list_activities(page=page, page_size=20)
                if not activities:
                    break
                reached_older = False
                for activity in activities:
                    recorded = _parse_time(activity.recorded_at or "")
                    if recorded is None:
                        skipped += 1
                        continue
                    if recorded < start:
                        reached_older = True
                        continue
                    if end and recorded.date() > end.date():
                        continue
                    if self.db.is_source_item_known(self.source_type, activity.ride_id):
                        skipped += 1
                        continue
                    selected.append(activity)
                    if len(selected) >= limit:
                        break
                if len(selected) >= limit or reached_older or len(activities) < 20:
                    break

            downloaded = 0
            for activity in reversed(selected):
                fit_path = self._client().download_fit(
                    activity.ride_id,
                    self.settings.incoming_dir,
                    self.settings.max_real_fit_upload_bytes,
                )
                remote_path = f"activity/{activity.ride_id}"
                write_source_sidecar(
                    fit_path,
                    SourceFileMetadata(
                        source_type=self.source_type,
                        source_external_id=activity.ride_id,
                        source_display_name=self.display_name,
                        source_original_filename=activity.original_filename,
                        source_remote_path=remote_path,
                        force_dry_run=dry_run,
                    ),
                )
                self.db.record_source_item(
                    source_type=self.source_type,
                    source_external_id=activity.ride_id,
                    source_original_filename=activity.original_filename,
                    source_remote_path=remote_path,
                )
                downloaded += 1
            return SourceSyncResult(
                True,
                "iGPSPORT historical import",
                (
                    f"Downloaded {downloaded} bounded historical FIT file(s) "
                    f"in {'dry-run' if dry_run else 'upload'} mode; skipped {skipped}."
                ),
                downloaded=downloaded,
                skipped=skipped,
            )
        except IGPSportError as exc:
            result = self._error_result("iGPSPORT historical import", exc)
            return SourceSyncResult(
                result.ok,
                result.title,
                result.message,
                requires_attention=result.requires_attention,
                retry_after_seconds=result.retry_after_seconds,
            )

    def _sync_incremental(self) -> SourceSyncResult:
        state = self.store.load_state()
        enabled_at = str(state.get("enabled_at") or "")
        if not enabled_at:
            enabled_at = _utc_now()
            self.store.save_state({"enabled_at": enabled_at})

        unknown: list[IGPSportActivity] = []
        skipped = 0
        stop = False
        for page in range(1, self.settings.igpsport_max_pages_per_poll + 1):
            activities = self._client().list_activities(page=page, page_size=20)
            if not activities:
                break
            for activity in activities:
                if self.db.is_source_item_known(self.source_type, activity.ride_id):
                    stop = True
                    break
                if not self._allowed_by_policy(activity, enabled_at):
                    self.db.record_source_item(
                        source_type=self.source_type,
                        source_external_id=activity.ride_id,
                        source_original_filename=activity.original_filename,
                    )
                    skipped += 1
                    continue
                unknown.append(activity)
            if stop or len(activities) < 20:
                break

        downloaded = 0
        for activity in reversed(unknown):
            fit_path = self._client().download_fit(
                activity.ride_id,
                self.settings.incoming_dir,
                self.settings.max_real_fit_upload_bytes,
            )
            write_source_sidecar(
                fit_path,
                SourceFileMetadata(
                    source_type=self.source_type,
                    source_external_id=activity.ride_id,
                    source_display_name=self.display_name,
                    source_original_filename=activity.original_filename,
                    source_remote_path=f"activity/{activity.ride_id}",
                ),
            )
            self.db.record_source_item(
                source_type=self.source_type,
                source_external_id=activity.ride_id,
                source_original_filename=activity.original_filename,
                source_remote_path=f"activity/{activity.ride_id}",
            )
            downloaded += 1

        self.store.save_state(
            {
                "last_successful_poll": _utc_now(),
                "latest_ride_id": unknown[0].ride_id if unknown else state.get("latest_ride_id"),
            }
        )
        return SourceSyncResult(
            True,
            "iGPSPORT sync",
            f"Downloaded {downloaded} new iGPSPORT FIT file(s); skipped {skipped} by import policy.",
            downloaded=downloaded,
            skipped=skipped,
        )

    def _allowed_by_policy(self, activity: IGPSportActivity, enabled_at: str) -> bool:
        profile = self.store.load_profile()
        mode = str(profile.get("import_mode") or self.settings.igpsport_import_mode)
        cutoff = enabled_at
        if mode == "since_date":
            cutoff = str(profile.get("cutoff_date") or "")
        if not cutoff:
            return True
        if not activity.recorded_at:
            return False
        activity_time = _parse_time(activity.recorded_at)
        cutoff_time = _parse_time(cutoff)
        return bool(activity_time and cutoff_time and activity_time >= cutoff_time)

    def _base_url(self) -> str:
        profile = self.store.load_profile()
        return str(profile.get("base_url") or self.settings.igpsport_base_url)

    def _client(self) -> IGPSportClient:
        if self._client_override is not None:
            return self._client_override
        base_url = self._base_url()
        if self._managed_client is None or self._managed_base_url != base_url:
            if self._managed_client is not None:
                self._managed_client.client.close()
            self._managed_client = IGPSportClient(self.store, base_url=base_url)
            self._managed_base_url = base_url
        return self._managed_client

    def _error_result(self, title: str, exc: IGPSportError) -> SourceResult:
        profile = self.store.load_profile()
        message = redact_sensitive_text(
            str(exc),
            (
                str(profile.get("username") or ""),
                str(profile.get("password") or ""),
                str(self.store.load_session().get("access_token") or ""),
            ),
        )
        return SourceResult(
            False,
            title,
            message,
            requires_attention=exc.requires_attention,
            retry_after_seconds=exc.retry_after_seconds,
        )


def validate_fit_file(path: Path) -> None:
    size = path.stat().st_size
    if size < 12:
        raise IGPSportError("iGPSPORT download was too small to be a FIT file.")
    with path.open("rb") as handle:
        header = handle.read(64)
    if b".FIT" not in header[:16]:
        lowered = header.lower()
        if b"<html" in lowered or b"<!doctype" in lowered or lowered.startswith(b"{"):
            raise IGPSportError("iGPSPORT returned a web or JSON response instead of a FIT file.")
        raise IGPSportError("iGPSPORT download did not contain a valid FIT signature.")
    try:
        from garmin_fit_sdk import Decoder, Stream

        decoder = Decoder(Stream.from_file(str(path)))
        _, errors = decoder.read()
        if errors:
            raise IGPSportError("iGPSPORT FIT validation reported decoder errors.")
    except IGPSportError:
        raise
    except Exception as exc:
        raise IGPSportError("iGPSPORT FIT download could not be parsed.") from exc


def _find_string(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item is not None and not isinstance(item, (dict, list)):
                text = str(item).strip()
                if text:
                    return text
        for item in value.values():
            found = _find_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_string(item, keys)
            if found:
                return found
    return ""


def _find_list(value: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, list):
                return item
        for item in value.values():
            found = _find_list(item, keys)
            if found:
                return found
    return value if isinstance(value, list) else []


def _safe_external_id(value: str) -> str:
    safe = "".join(character for character in value if character.isalnum() or character in "-_")
    if not safe:
        raise IGPSportError("iGPSPORT returned an invalid activity identifier.")
    return safe[:128]


def _retry_after_seconds(value: str | None) -> int | None:
    if value and value.strip().isdigit():
        return min(max(int(value), 1), 21_600)
    return None


def _login_rejection_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        code = payload.get("code")
        server_message = str(payload.get("message") or payload.get("msg") or "").lower()
        if code == 1002 or "password error" in server_message:
            return (
                "iGPSPORT rejected the account identifier or password. "
                "Confirm the same details work in the iGPSPORT app and that the "
                "selected account region is correct."
            )
    return (
        "iGPSPORT rejected the saved account or session. "
        "Check the account details and selected account region."
    )


def _is_expired(value: str) -> bool:
    parsed = _parse_time(value)
    return parsed is not None and parsed <= datetime.now(UTC)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        if normalized.isdigit():
            timestamp = int(normalized)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=UTC)
        for date_format in ("%Y.%m.%d", "%Y.%m.%d %H:%M:%S"):
            try:
                return datetime.strptime(normalized, date_format).replace(tzinfo=UTC)
            except ValueError:
                continue
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
