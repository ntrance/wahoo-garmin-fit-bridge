from __future__ import annotations

import hashlib
import json
import logging
import tempfile
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

LOGIN_PATH = "account/login"
ACTIVITY_LIST_PATH = "activity/query"
DOWNLOAD_URL_PATH = "activity/detail/download"
USER_AGENT = "fit-to-garmin-bridge/0.1 (+https://github.com/ntrance/wahoo-garmin-fit-bridge)"


class CorosError(RuntimeError):
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
class CorosActivity:
    label_id: str
    sport_type: int
    recorded_at: str | None
    original_filename: str | None


class CorosStore:
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
            raise ValueError("COROS account identifier is required.")
        if not saved_password:
            raise ValueError("COROS password is required.")
        if urlparse(normalized_base_url).scheme != "https":
            raise ValueError("COROS base URL must use HTTPS.")
        if import_mode not in {"new_only", "since_date"}:
            raise ValueError("Unsupported COROS import mode.")
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

    def save_session(
        self,
        token: str,
        user_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        write_private_text(
            self.session_path,
            json.dumps(
                {
                    "access_token": token,
                    "user_id": user_id,
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


class CorosClient:
    def __init__(
        self,
        store: CorosStore,
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
            raise CorosError(
                "COROS profile is incomplete.",
                requires_attention=True,
            )
        pwd_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        response = self._request(
            "POST",
            LOGIN_PATH,
            allow_auth_failure=True,
            json={
                "account": username,
                "pwd": pwd_hash,
                "accountType": 2,
            },
        )
        if response.status_code in {401, 403}:
            raise CorosError(
                _login_rejection_message(response),
                requires_attention=True,
            )
        payload = self._json(response, "COROS login returned invalid JSON.")
        code = str(payload.get("code") or "")
        if code and code != "0000" and code != "0":
            msg = str(payload.get("message") or payload.get("msg") or f"Error code {code}")
            raise CorosError(f"COROS login failed: {msg}", requires_attention=True)

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = _find_string(data, ("accessToken", "access_token", "token"))
        if not token:
            raise CorosError(
                "COROS login succeeded but returned no access token.",
                requires_attention=True,
            )
        user_id = _find_string(data, ("userId", "user_id", "id"))
        expires_at = _find_string(data, ("expiresAt", "expires_at", "expiration"))
        self.store.save_session(token, user_id=user_id, expires_at=expires_at)
        return token

    def token(self) -> str:
        session = self.store.load_session()
        token = str(session.get("access_token") or "")
        if token and not _is_expired(str(session.get("expires_at") or "")):
            return token
        return self.login()

    def list_activities(self, page: int = 1, page_size: int = 20) -> list[CorosActivity]:
        response = self._authenticated_request(
            "POST",
            ACTIVITY_LIST_PATH,
            json={
                "pageNumber": page,
                "size": page_size,
                "modeList": [
                    100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                    110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120,
                ],
            },
        )
        payload = self._json(response, "COROS activity list returned invalid JSON.")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        rows = _find_list(data, ("dataList", "rows", "records", "list", "activities"))
        activities: list[CorosActivity] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            label_id = _find_string(row, ("labelId", "label_id", "activityId", "id"))
            if not label_id:
                continue
            sport_type = int(row.get("sportType") or row.get("sport_type") or 100)
            raw_time = row.get("startTime") or row.get("start_time") or row.get("createTime")
            recorded_at = _format_recorded_time(raw_time)
            activities.append(
                CorosActivity(
                    label_id=label_id,
                    sport_type=sport_type,
                    recorded_at=recorded_at,
                    original_filename=_find_string(
                        row,
                        ("name", "activityName", "title", "fileName", "filename"),
                    ),
                )
            )
        return activities

    def get_download_url(self, label_id: str, sport_type: int = 100) -> str:
        response = self._authenticated_request(
            "POST",
            DOWNLOAD_URL_PATH,
            json={
                "labelId": label_id,
                "sportType": sport_type,
                "fileType": "fit",
            },
        )
        payload = self._json(response, "COROS download response returned invalid JSON.")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        url = _find_string(data, ("fileUrl", "file_url", "downloadUrl", "download_url", "url"))
        if not url and isinstance(payload.get("data"), str):
            url = payload["data"].strip()
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CorosError("COROS returned an invalid FIT download URL.")
        return url

    def download_fit(
        self,
        label_id: str,
        sport_type: int,
        incoming_dir: Path,
        max_bytes: int,
    ) -> Path:
        url = self.get_download_url(label_id, sport_type)
        incoming_dir.mkdir(parents=True, exist_ok=True)
        final_path = incoming_dir / f"coros_{_safe_external_id(label_id)}.fit"
        temp_path: Path | None = None
        try:
            with self.client.stream("GET", url) as stream:
                if stream.status_code >= 400:
                    raise CorosError(
                        f"COROS download failed with status {stream.status_code}."
                    )
                total = 0
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    delete=False,
                    dir=incoming_dir,
                    prefix=".coros-",
                    suffix=".tmp",
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    for chunk in stream.iter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise CorosError(
                                f"COROS FIT payload exceeded limit of {max_bytes} bytes."
                            )
                        temp_file.write(chunk)
            temp_path.replace(final_path)
            return final_path
        except Exception:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

    def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = self.token()
        headers = {"accesstoken": token}
        response = self._request(
            method,
            path,
            headers=headers,
            params=params,
            json=json,
            allow_auth_failure=True,
        )
        if response.status_code == 401 or _is_token_invalid_payload(response):
            logger.info("COROS session token expired or invalid; re-authenticating.")
            self.store.clear_session()
            token = self.login()
            headers = {"accesstoken": token}
            response = self._request(
                method,
                path,
                headers=headers,
                params=params,
                json=json,
                allow_auth_failure=False,
            )
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        allow_auth_failure: bool = False,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        try:
            response = self.client.request(
                method,
                url,
                headers=merged_headers,
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            redacted = redact_sensitive_text(str(exc))
            raise CorosError(f"COROS request failed: {redacted}") from exc

        if response.status_code in {429, 503}:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise CorosError(
                "COROS service is busy. Polling backed off.",
                retry_after_seconds=retry_after or 300,
            )
        if response.status_code in {401, 403} and not allow_auth_failure:
            raise CorosError(
                "COROS authentication failed. Check credentials.",
                requires_attention=True,
            )
        if response.status_code >= 500:
            raise CorosError(
                f"COROS service error ({response.status_code}). Polling backed off.",
                retry_after_seconds=300,
            )
        if response.status_code >= 400 and not allow_auth_failure:
            raise CorosError(f"COROS request failed with status {response.status_code}.")
        return response

    @staticmethod
    def _json(response: httpx.Response, error_message: str) -> dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except (ValueError, TypeError) as exc:
            raise CorosError(error_message) from exc


class CorosSource:
    source_type = "coros"
    display_name = "COROS Cloud"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        store: CorosStore | None = None,
        client: CorosClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.poll_seconds = settings.coros_poll_seconds
        self.store = store or CorosStore(settings.coros_config_dir)
        self._client = client

    def is_enabled(self) -> bool:
        return self.settings.coros_source_enabled

    def is_configured(self) -> bool:
        profile = self.store.load_profile()
        return bool(profile.get("username") and profile.get("password"))

    def test_connection(self) -> SourceResult:
        if not self.is_configured():
            return SourceResult(
                False,
                "COROS connection test",
                "COROS source is not configured.",
                requires_attention=True,
            )
        try:
            client = self._get_client()
            client.login()
            activities = client.list_activities(page=1, page_size=1)
            msg = "COROS login succeeded."
            if activities:
                msg = f"COROS login succeeded. Found {len(activities)} recent activity."
            return SourceResult(True, "COROS connection test", msg)
        except CorosError as exc:
            return SourceResult(
                False,
                "COROS connection test",
                str(exc),
                requires_attention=exc.requires_attention,
                retry_after_seconds=exc.retry_after_seconds,
            )
        except Exception as exc:  # pragma: no cover
            redacted = redact_sensitive_text(str(exc))
            return SourceResult(False, "COROS connection test", f"Unexpected error: {redacted}")

    def sync_to_incoming(self, *, historical: bool = False) -> SourceSyncResult:
        if not self.is_enabled():
            return SourceSyncResult(
                False,
                "COROS sync",
                "COROS source is disabled.",
            )
        if not self.is_configured():
            return SourceSyncResult(
                False,
                "COROS sync",
                "COROS source is not configured.",
                requires_attention=True,
            )
        try:
            return self._sync_incremental()
        except CorosError as exc:
            return SourceSyncResult(
                False,
                "COROS sync",
                str(exc),
                requires_attention=exc.requires_attention,
                retry_after_seconds=exc.retry_after_seconds,
            )
        except Exception as exc:  # pragma: no cover
            redacted = redact_sensitive_text(str(exc))
            return SourceSyncResult(False, "COROS sync", f"Unexpected error: {redacted}")

    def _sync_incremental(self) -> SourceSyncResult:
        state = self.store.load_state()
        enabled_at = str(state.get("enabled_at") or "")
        if not enabled_at:
            enabled_at = _utc_now()
            self.store.save_state({"enabled_at": enabled_at})

        client = self._get_client()
        client.token()
        unknown: list[CorosActivity] = []
        skipped = 0
        stop = False

        for page in range(1, self.settings.coros_max_pages_per_poll + 1):
            activities = client.list_activities(page=page, page_size=20)
            if not activities:
                break
            for activity in activities:
                if self.db.is_source_item_known(self.source_type, activity.label_id):
                    stop = True
                    break
                if not self._allowed_by_policy(activity, enabled_at):
                    self.db.record_source_item(
                        source_type=self.source_type,
                        source_external_id=activity.label_id,
                        source_original_filename=activity.original_filename,
                    )
                    skipped += 1
                    continue
                unknown.append(activity)
            if stop or len(activities) < 20:
                break

        downloaded = 0
        for activity in reversed(unknown):
            fit_path = client.download_fit(
                activity.label_id,
                activity.sport_type,
                self.settings.incoming_dir,
                self.settings.max_real_fit_upload_bytes,
            )
            write_source_sidecar(
                fit_path,
                SourceFileMetadata(
                    source_type=self.source_type,
                    source_external_id=activity.label_id,
                    source_display_name=self.display_name,
                    source_original_filename=activity.original_filename,
                ),
            )
            self.db.record_source_item(
                source_type=self.source_type,
                source_external_id=activity.label_id,
                source_original_filename=activity.original_filename,
            )
            downloaded += 1

        self.store.save_state(
            {
                "last_successful_poll": _utc_now(),
                "latest_label_id": unknown[0].label_id if unknown else state.get("latest_label_id"),
            }
        )
        msg = f"Synced {downloaded} new ride(s) from COROS."
        if skipped:
            msg += f" Skipped {skipped} older ride(s)."
        return SourceSyncResult(
            True,
            "COROS sync",
            msg,
            downloaded=downloaded,
            skipped=skipped,
        )

    def _allowed_by_policy(self, activity: CorosActivity, enabled_at: str) -> bool:
        profile = self.store.load_profile()
        import_mode = str(profile.get("import_mode") or self.settings.coros_import_mode)
        recorded = _parse_time(activity.recorded_at or "")
        if recorded is None:
            return True
        if import_mode == "since_date":
            cutoff = str(profile.get("cutoff_date") or "")
            cutoff_dt = _parse_time(f"{cutoff}T00:00:00Z") if cutoff else None
            return cutoff_dt is None or recorded >= cutoff_dt
        enabled_dt = _parse_time(enabled_at)
        return enabled_dt is None or recorded >= enabled_dt

    def supports_remote_delete(self) -> bool:
        return False

    def delete_remote_activity(self, external_id: str) -> SourceResult:
        del external_id
        return SourceResult(
            False,
            "COROS delete",
            "COROS source does not support remote activity deletion.",
        )

    def _get_client(self) -> CorosClient:
        if self._client is not None:
            return self._client
        profile = self.store.load_profile()
        base_url = str(profile.get("base_url") or self.settings.coros_base_url)
        return CorosClient(self.store, base_url=base_url)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return dt <= datetime.now(UTC)
    except ValueError:
        return False


def _find_string(data: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(data, dict):
        return ""
    for k in keys:
        v = data.get(k)
        if v is not None:
            s = str(v).strip()
            if s:
                return s
    return ""


def _find_list(data: Any, keys: tuple[str, ...]) -> list[Any]:
    if not isinstance(data, dict):
        return []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def _format_recorded_time(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Handle ms vs s timestamp
        ts = raw / 1000.0 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            return None
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if "T" in s:
            return s
        try:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return s
    return None


def _safe_external_id(external_id: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in external_id)


def _login_rejection_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            msg = str(data.get("message") or data.get("msg") or "")
            if msg:
                return f"COROS rejected credentials: {msg}"
    except Exception:
        pass
    return f"COROS rejected credentials (HTTP {response.status_code})."


def _is_token_invalid_payload(response: httpx.Response) -> bool:
    try:
        data = response.json()
        if isinstance(data, dict):
            code = str(data.get("code") or "")
            msg = str(data.get("message") or data.get("msg") or "").lower()
            return code in {"401", "1001", "1002"} or "token" in msg or "unauthorized" in msg
    except Exception:
        pass
    return False


def _parse_retry_after(header_value: str | None) -> int | None:
    if not header_value:
        return None
    try:
        return max(1, int(header_value.strip()))
    except ValueError:
        return None
