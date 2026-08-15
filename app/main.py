from __future__ import annotations

import asyncio
import math
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import Database
from app.dropbox_oauth import complete_dropbox_oauth, start_dropbox_oauth
from app.garmin_upload import friendly_upload_error
from app.fit_preview import (
    _build_activity_preview_cached,
    build_activity_preview,
    get_disk_preview_path,
)
from app.garmin_device import (
    find_garmin_target,
    garmin_device_presets,
    garmin_product_display_name,
    load_detected_devices,
    save_uploaded_real_fit,
    scan_real_fit_devices,
)
from app.jobs import BridgeService
from app.logging_config import configure_logging
from app.source_manager import SourceManager
from app.source_scheduler import SourceScheduler
from app.sources.igpsport import IGPSportStore
from app.security import (
    RateLimiter,
    csrf_token,
    sign_session,
    verify_csrf,
    verify_password,
    verify_session,
)
from app.setup_status import (
    build_setup_status,
    clear_garmin_session_pause,
    create_garmin_session_token,
    save_runtime_config,
    test_dropbox,
    test_garmin_upload,
)
from app.settings import IGPSPORT_REGIONS, Settings
from app.setup_status import save_dropbox_auth, save_garmin_profile
from concurrent.futures import ThreadPoolExecutor

from app.hardware import get_hardware_profile, normalize_timezone_name
from app.system_metrics import get_system_status, run_system_benchmark
from app.update_checker import check_for_update
from app.version import get_app_version

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db)
    source_manager = SourceManager(settings, db)
    service.source_manager = source_manager
    rate_limiter = RateLimiter(
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )
    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        hw = get_hardware_profile()
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=hw.max_workers)
        loop.set_default_executor(executor)

        runtime_settings = app_instance.state.settings
        runtime_service = app_instance.state.service
        runtime_settings.validate_security()
        runtime_service.setup()
        if start_background:
            scheduler = SourceScheduler(
                app_instance.state.source_manager,
                runtime_service,
            )
            app_instance.state.scheduler = scheduler
            app_instance.state.scan_task = asyncio.create_task(scheduler.run_forever())
            app_instance.state.update_check_task = asyncio.create_task(
                _update_check_loop(app_instance)
            )
        try:
            yield
        finally:
            task = getattr(app_instance.state, "scan_task", None)
            if task is not None:
                task.cancel()
            update_task = getattr(app_instance.state, "update_check_task", None)
            if update_task is not None:
                update_task.cancel()
            executor.shutdown(wait=False)

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.state.settings = settings
    app.state.db = db
    app.state.service = service
    app.state.source_manager = source_manager
    app.state.app_version = get_app_version()
    app.state.update_status = None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        runtime_settings = request.app.state.settings
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "style-src 'self' https://cdn.jsdelivr.net https://unpkg.com 'unsafe-inline'; "
            "script-src 'self' https://cdn.jsdelivr.net https://unpkg.com 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://tile.openstreetmap.org; "
            "connect-src 'self' https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://api.github.com; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        if runtime_settings.session_cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    def require_auth(request: Request) -> None:
        runtime_settings = request.app.state.settings
        if not runtime_settings.web_auth_enabled:
            return
        session_cookie = request.cookies.get(runtime_settings.session_cookie_name, "")
        session = verify_session(session_cookie, runtime_settings.session_secret_key)
        if session is None or not secrets.compare_digest(
            session.username, runtime_settings.web_username
        ):
            raise_auth(request)
        request.state.auth_session = session

    async def require_csrf(request: Request, _auth: None = Depends(require_auth)) -> None:
        runtime_settings = request.app.state.settings
        if not runtime_settings.web_auth_enabled:
            return
        form = await request.form()
        token = str(form.get("csrf_token", ""))
        session_cookie = request.cookies.get(runtime_settings.session_cookie_name, "")
        if not verify_csrf(session_cookie, token, runtime_settings.session_secret_key):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    def context(request: Request) -> dict[str, object]:
        runtime_settings = request.app.state.settings
        session_cookie = request.cookies.get(runtime_settings.session_cookie_name, "")
        session = (
            verify_session(session_cookie, runtime_settings.session_secret_key)
            if session_cookie
            else None
        )
        return {
            "request": request,
            "settings": runtime_settings,
            "authenticated": session is not None,
            "csrf_token": (
                csrf_token(session_cookie, runtime_settings.session_secret_key)
                if session
                else ""
            ),
            "app_version": request.app.state.app_version,
            "update_status": getattr(request.app.state, "update_status", None),
        }

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            context(request) | {"next": _safe_next(next), "error": None},
        )

    @app.post("/login")
    async def login(request: Request) -> Response:
        runtime_settings = request.app.state.settings
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        next_url = _safe_next(str(form.get("next", "/")))
        client_key = request.client.host if request.client else "unknown"

        if rate_limiter.is_limited(client_key):
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request) | {"next": next_url, "error": "Too many failed attempts. Try again later."},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        username_ok = secrets.compare_digest(username, runtime_settings.web_username)
        password_ok = verify_password(
            password,
            runtime_settings.web_password,
            runtime_settings.web_password_hash,
        )
        if not (username_ok and password_ok):
            rate_limiter.record_failure(client_key)
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request) | {"next": next_url, "error": "Invalid username or password."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        rate_limiter.clear(client_key)
        response = RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            runtime_settings.session_cookie_name,
            sign_session(
                runtime_settings.web_username,
                runtime_settings.session_secret_key,
                runtime_settings.session_max_age_seconds,
            ),
            max_age=runtime_settings.session_max_age_seconds,
            httponly=True,
            secure=runtime_settings.session_cookie_secure,
            samesite="strict",
        )
        return response

    @app.post("/logout")
    async def logout(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(app.state.settings.session_cookie_name)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        page: int = 1,
        _auth: None = Depends(require_auth),
    ) -> HTMLResponse:
        runtime_db = request.app.state.db
        stats = runtime_db.stats()
        page_size = 10
        current_page = max(1, page)
        activities, total_count = runtime_db.list_paginated(page=current_page, page_size=page_size)
        total_pages = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 1
        if current_page > total_pages:
            current_page = total_pages
            activities, total_count = runtime_db.list_paginated(page=current_page, page_size=page_size)
        grouped_activities = group_activities_by_month(activities)
        cleanup_activities = runtime_db.list_cleanup_candidates(100)
        dashboard_message = getattr(app.state, "dashboard_message", None)
        app.state.dashboard_message = None
        return templates.TemplateResponse(
            request,
            "index.html",
            context(request)
            | {
                "stats": stats,
                "activities": activities,
                "grouped_activities": grouped_activities,
                "current_page": current_page,
                "total_pages": total_pages,
                "total_count": total_count,
                "page_size": page_size,
                "cleanup_activities": cleanup_activities,
                "dashboard_message": dashboard_message,
                "source_statuses": request.app.state.source_manager.statuses(),
                "update_status": getattr(request.app.state, "update_status", None),
            },
        )

    @app.get("/activity/{activity_id}", response_class=HTMLResponse)
    async def activity_detail(
        request: Request,
        activity_id: int,
        _auth: None = Depends(require_auth),
    ) -> HTMLResponse:
        activity = request.app.state.db.get_activity(activity_id)
        if activity is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        activity_messages = getattr(app.state, "activity_messages", {})
        activity_message = activity_messages.pop(activity_id, None)
        app.state.activity_messages = activity_messages
        preview = await asyncio.to_thread(
            build_activity_preview,
            activity,
            request.app.state.settings.previews_dir,
        )
        return templates.TemplateResponse(
            request,
            "activity.html",
            context(request)
            | {
                "activity": _activity_for_display(activity),
                "source_references": request.app.state.db.list_source_items_for_activity(
                    activity_id
                ),
                "activity_message": activity_message,
                "preview": preview,
            },
        )

    @app.post("/activity/{activity_id}/retry")
    async def retry(activity_id: int, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        await _run_activity_action(app, app.state.service, activity_id, False)
        return RedirectResponse(f"/activity/{activity_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/activity/{activity_id}/ignore")
    async def ignore(activity_id: int, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        app.state.service.mark_ignored(activity_id)
        return RedirectResponse(f"/activity/{activity_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/activity/{activity_id}/reprocess")
    async def reprocess(activity_id: int, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        await _run_activity_action(app, app.state.service, activity_id, True)
        return RedirectResponse(f"/activity/{activity_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/activity/{activity_id}/refresh-preview")
    async def refresh_preview(
        request: Request,
        activity_id: int,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        activity = request.app.state.db.get_activity(activity_id)
        if activity is not None:
            previews_dir = request.app.state.settings.previews_dir
            disk_path = get_disk_preview_path(activity_id, previews_dir)
            if disk_path and disk_path.exists():
                try:
                    disk_path.unlink()
                except Exception:
                    pass
            _build_activity_preview_cached.cache_clear()
            await asyncio.to_thread(build_activity_preview, activity, previews_dir)
        return RedirectResponse(f"/activity/{activity_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/activity/{activity_id}/delete-dropbox")
    async def delete_dropbox_file(
        request: Request,
        activity_id: int,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        form = await request.form()
        next_url = _safe_next(str(form.get("next", "/")))
        title = "Delete Dropbox file"
        try:
            activity = await asyncio.to_thread(
                request.app.state.service.delete_dropbox_file, activity_id
            )
            ok = activity["status"] == "dropbox_deleted"
            output = str(activity.get("garmin_response") or "Dropbox cleanup finished.")
        except (RuntimeError, sqlite3.OperationalError, KeyError) as exc:
            ok = False
            output = _friendly_action_error(exc)

        if next_url.startswith(f"/activity/{activity_id}"):
            _set_activity_message(app, activity_id, ok, title, output)
        else:
            app.state.dashboard_message = {"ok": ok, "title": title, "output": output}
        return RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/activity/{activity_id}/reprocess")
    async def reprocess_get(activity_id: int, _auth: None = Depends(require_auth)) -> RedirectResponse:
        _set_activity_message(
            app,
            activity_id,
            False,
            "Reprocess",
            "Use the Reprocess button on this page. Direct links do not start uploads.",
        )
        return RedirectResponse(f"/activity/{activity_id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/rescan")
    async def rescan(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        try:
            runtime_service = app.state.service
            sync_result = await asyncio.to_thread(
                app.state.source_manager.sync_all,
                manual=True,
            )
            scan_result = await asyncio.to_thread(runtime_service.scan_once)
            source_output = "\n".join(
                result.message for result in sync_result.values()
            )
            app.state.dashboard_message = {
                "ok": all(result.ok for result in sync_result.values()),
                "title": "Rescan",
                "output": (
                    f"{source_output}\n"
                    f"Discovered {scan_result['discovered']} file(s). "
                    f"Processed {scan_result['processed']} file(s)."
                ),
            }
        except (RuntimeError, sqlite3.OperationalError) as exc:
            app.state.dashboard_message = {
                "ok": False,
                "title": "Rescan",
                "output": _friendly_action_error(exc),
            }
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/activities/confirm-imported")
    async def confirm_imported(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        updated = app.state.db.mark_dry_runs_already_on_garmin()
        app.state.dashboard_message = {
            "ok": True,
            "title": "Imported history confirmed",
            "output": f"Marked {updated} imported activity file(s) as already on Garmin.",
        }
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        runtime_settings = request.app.state.settings
        config = {
            "Dropbox remote name": runtime_settings.rclone_remote,
            "Dropbox path": runtime_settings.dropbox_wahoo_path,
            "Incoming folder": runtime_settings.incoming_dir,
            "Processing folder": runtime_settings.processing_dir,
            "Uploaded folder": runtime_settings.uploaded_dir,
            "Duplicate folder": runtime_settings.duplicate_dir,
            "Failed folder": runtime_settings.failed_dir,
            "Poll interval": f"{runtime_settings.poll_seconds} seconds",
            "Dropbox source": (
                "enabled" if runtime_settings.dropbox_source_enabled else "disabled"
            ),
            "iGPSPORT source": (
                "enabled" if runtime_settings.igpsport_source_enabled else "disabled"
            ),
            "Garmin device": runtime_settings.garmin_device_name,
            "Garmin Unit ID configured": (
                "yes" if runtime_settings.garmin_unit_id else "no"
            ),
            "Garmin profile": runtime_settings.garmin_profile_name,
            "Garmin config": runtime_settings.garmin_config_dir,
            "Dry-run mode": "enabled" if runtime_settings.dry_run else "disabled",
            "Web auth": (
                "enabled" if runtime_settings.web_auth_enabled else "disabled"
            ),
            "Password hash configured": (
                "yes" if runtime_settings.web_password_hash else "no"
            ),
            "Secure cookies": (
                "enabled" if runtime_settings.session_cookie_secure else "disabled"
            ),
            "Session lifetime": f"{runtime_settings.session_max_age_seconds} seconds",
            "Login rate limit": (
                f"{runtime_settings.login_rate_limit_attempts} attempts per "
                f"{runtime_settings.login_rate_limit_window_seconds} seconds"
            ),
        }
        saved_igpsport_profile = IGPSportStore(
            runtime_settings.igpsport_config_dir
        ).load_profile()
        igpsport_profile = {
            "username": saved_igpsport_profile.get("username", ""),
            "base_url": saved_igpsport_profile.get(
                "base_url",
                runtime_settings.igpsport_base_url,
            ),
            "import_mode": saved_igpsport_profile.get(
                "import_mode",
                runtime_settings.igpsport_import_mode,
            ),
            "cutoff_date": saved_igpsport_profile.get("cutoff_date", ""),
            "password_saved": bool(saved_igpsport_profile.get("password")),
        }
        return templates.TemplateResponse(
            request,
            "config.html",
            context(request)
            | {
                "config": config,
                "setup": build_setup_status(runtime_settings),
                "detected_devices": load_detected_devices(runtime_settings),
                "garmin_presets": garmin_device_presets(),
                "setup_message": getattr(app.state, "setup_message", None),
                "dropbox_oauth": getattr(app.state, "dropbox_oauth", None),
                "source_statuses": request.app.state.source_manager.statuses(),
                "igpsport_profile": igpsport_profile,
                "igpsport_regions": IGPSPORT_REGIONS,
                "status": get_system_status(runtime_settings, app.state.db),
            },
        )

    @app.post("/config/dropbox/oauth/start")
    async def start_dropbox_oauth_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        previous_session = getattr(app.state, "dropbox_oauth", None)
        if previous_session is not None and previous_session.process.poll() is None:
            previous_session.process.terminate()
        session, result = await asyncio.to_thread(
            start_dropbox_oauth, app.state.settings
        )
        app.state.setup_message = result.__dict__
        if session is not None:
            app.state.dropbox_oauth = session
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/dropbox/oauth/complete")
    async def complete_dropbox_oauth_route(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        runtime_settings = request.app.state.settings
        session = getattr(app.state, "dropbox_oauth", None)
        if session is None:
            app.state.setup_message = {
                "ok": False,
                "title": "Dropbox OAuth",
                "output": "Start Dropbox setup first.",
            }
            return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)
        form = await request.form()
        remote_name = _form_text(
            form.get("rclone_remote"), runtime_settings.rclone_remote
        )
        dropbox_path = _form_text(
            form.get("dropbox_wahoo_path"), runtime_settings.dropbox_wahoo_path
        )
        result = await asyncio.to_thread(
            complete_dropbox_oauth,
            runtime_settings,
            session,
            str(form.get("dropbox_callback_url", "")),
            remote_name,
            dropbox_path,
        )
        app.state.setup_message = result.__dict__
        if result.ok:
            save_runtime_config(
                runtime_settings,
                {
                    "RCLONE_REMOTE": remote_name,
                    "DROPBOX_WAHOO_PATH": dropbox_path,
                    "GARMIN_PROFILE_NAME": runtime_settings.garmin_profile_name,
                    "GARMIN_UNIT_ID": runtime_settings.garmin_unit_id,
                    "DRY_RUN": "true" if runtime_settings.dry_run else "false",
                },
            )
            updated_settings = replace(
                runtime_settings,
                rclone_remote=remote_name,
                dropbox_wahoo_path=dropbox_path,
            )
            updated_db = Database(updated_settings.sqlite_path)
            updated_service = BridgeService(updated_settings, updated_db)
            updated_service.setup()
            app.state.settings = updated_settings
            app.state.db = updated_db
            app.state.service = updated_service
            app.state.dropbox_oauth = None
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/dropbox/oauth/cancel")
    async def cancel_dropbox_oauth_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        session = getattr(app.state, "dropbox_oauth", None)
        if session is not None and session.process.poll() is None:
            session.process.terminate()
        app.state.dropbox_oauth = None
        app.state.setup_message = {
            "ok": True,
            "title": "Dropbox OAuth",
            "output": "Dropbox setup was cancelled.",
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/save")
    async def save_config(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        runtime_settings = request.app.state.settings
        form = await request.form()
        updates = {
            "RCLONE_REMOTE": _form_text(
                form.get("rclone_remote"), runtime_settings.rclone_remote
            ),
            "DROPBOX_WAHOO_PATH": _form_text(
                form.get("dropbox_wahoo_path"),
                runtime_settings.dropbox_wahoo_path,
            ),
            "GARMIN_PROFILE_NAME": _form_text(
                form.get("garmin_profile_name"),
                runtime_settings.garmin_profile_name,
            ),
            "GARMIN_UNIT_ID": _form_text(
                form.get("garmin_unit_id"), runtime_settings.garmin_unit_id
            ),
            "DRY_RUN": "true" if form.get("dry_run") == "on" else "false",
            "DROPBOX_SOURCE_ENABLED": (
                "true" if form.get("dropbox_source_enabled") == "on" else "false"
            ),
            "IGPSPORT_SOURCE_ENABLED": (
                "true" if form.get("igpsport_source_enabled") == "on" else "false"
            ),
            "DROPBOX_POLL_SECONDS": _form_text(
                form.get("dropbox_poll_seconds"),
                str(runtime_settings.dropbox_poll_seconds),
            ),
            "IGPSPORT_POLL_SECONDS": _form_text(
                form.get("igpsport_poll_seconds"),
                str(runtime_settings.igpsport_poll_seconds),
            ),
            "SMART_SCHEDULING_ENABLED": "true" if form.get("smart_scheduling_enabled") == "on" else "false",
            "QUIET_WINDOW_START": _form_text(form.get("quiet_window_start"), str(runtime_settings.quiet_window_start)),
            "QUIET_WINDOW_END": _form_text(form.get("quiet_window_end"), str(runtime_settings.quiet_window_end)),
            "QUIET_WINDOW_POLL_MINS": _form_text(form.get("quiet_window_poll_mins"), str(runtime_settings.quiet_window_poll_mins)),
            "PEAK_WINDOW_START": _form_text(form.get("peak_window_start"), str(runtime_settings.peak_window_start)),
            "PEAK_WINDOW_END": _form_text(form.get("peak_window_end"), str(runtime_settings.peak_window_end)),
            "PEAK_WINDOW_POLL_MINS": _form_text(form.get("peak_window_poll_mins"), str(runtime_settings.peak_window_poll_mins)),
            "DAYLIGHT_WINDOW_POLL_MINS": _form_text(form.get("daylight_window_poll_mins"), str(runtime_settings.daylight_window_poll_mins)),
            "TZ": normalize_timezone_name(_form_text(form.get("timezone"), runtime_settings.timezone)),
            "TIMEZONE": normalize_timezone_name(_form_text(form.get("timezone"), runtime_settings.timezone)),
        }
        save_runtime_config(runtime_settings, updates)
        updated_settings = replace(
            runtime_settings,
            rclone_remote=updates["RCLONE_REMOTE"],
            dropbox_wahoo_path=updates["DROPBOX_WAHOO_PATH"],
            garmin_profile_name=updates["GARMIN_PROFILE_NAME"],
            garmin_unit_id=updates["GARMIN_UNIT_ID"],
            dry_run=updates["DRY_RUN"] == "true",
            dropbox_source_enabled=updates["DROPBOX_SOURCE_ENABLED"] == "true",
            igpsport_source_enabled=updates["IGPSPORT_SOURCE_ENABLED"] == "true",
            dropbox_poll_seconds=max(int(updates["DROPBOX_POLL_SECONDS"]), 10),
            igpsport_poll_seconds=max(
                int(updates["IGPSPORT_POLL_SECONDS"]),
                runtime_settings.igpsport_min_poll_seconds,
            ),
            timezone=updates["TZ"],
            smart_scheduling_enabled=updates["SMART_SCHEDULING_ENABLED"] == "true",
            quiet_window_start=int(updates["QUIET_WINDOW_START"]),
            quiet_window_end=int(updates["QUIET_WINDOW_END"]),
            quiet_window_poll_mins=int(updates["QUIET_WINDOW_POLL_MINS"]),
            peak_window_start=int(updates["PEAK_WINDOW_START"]),
            peak_window_end=int(updates["PEAK_WINDOW_END"]),
            peak_window_poll_mins=int(updates["PEAK_WINDOW_POLL_MINS"]),
            daylight_window_poll_mins=int(updates["DAYLIGHT_WINDOW_POLL_MINS"]),
        )
        updated_db = Database(updated_settings.sqlite_path)
        updated_service = BridgeService(updated_settings, updated_db)
        updated_source_manager = SourceManager(updated_settings, updated_db)
        updated_service.source_manager = updated_source_manager
        updated_service.setup()
        app.state.settings = updated_settings
        app.state.db = updated_db
        app.state.service = updated_service
        app.state.source_manager = updated_source_manager
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.reconfigure(updated_source_manager, updated_service)
        app.state.setup_message = {
            "ok": True,
            "title": "Settings saved",
            "output": "Saved to runtime config. Background source polling has been updated.",
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/igpsport/save")
    async def save_igpsport_profile_route(
        request: Request,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        runtime_settings = request.app.state.settings
        form = await request.form()
        store = IGPSportStore(runtime_settings.igpsport_config_dir)
        try:
            base_url = _form_text(
                form.get("igpsport_base_url"),
                runtime_settings.igpsport_base_url,
            ).rstrip("/")
            if base_url not in {region[2] for region in IGPSPORT_REGIONS}:
                raise ValueError("Select a supported iGPSPORT account region.")
            store.save_profile(
                username=_form_text(form.get("igpsport_username"), ""),
                password=_form_text(form.get("igpsport_password"), ""),
                base_url=base_url,
                import_mode=_form_text(
                    form.get("igpsport_import_mode"),
                    runtime_settings.igpsport_import_mode,
                ),
                cutoff_date=_form_text(form.get("igpsport_cutoff_date"), ""),
            )
            app.state.setup_message = {
                "ok": True,
                "title": "iGPSPORT profile",
                "output": "Saved the iGPSPORT profile without displaying stored credentials.",
            }
        except ValueError as exc:
            app.state.setup_message = {
                "ok": False,
                "title": "iGPSPORT profile",
                "output": str(exc),
            }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/igpsport/clear-session")
    async def clear_igpsport_session_route(
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        IGPSportStore(app.state.settings.igpsport_config_dir).clear_session()
        app.state.setup_message = {
            "ok": True,
            "title": "iGPSPORT session",
            "output": "Cleared the saved iGPSPORT session token.",
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/igpsport/delete")
    async def delete_igpsport_profile_route(
        request: Request,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        form = await request.form()
        if _form_text(form.get("confirm"), "") != "DELETE":
            app.state.setup_message = {
                "ok": False,
                "title": "Delete iGPSPORT profile",
                "output": 'Enter "DELETE" to remove the saved profile and session.',
            }
        else:
            IGPSportStore(app.state.settings.igpsport_config_dir).delete_profile()
            app.state.setup_message = {
                "ok": True,
                "title": "Delete iGPSPORT profile",
                "output": "Deleted the saved iGPSPORT profile and session.",
            }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/config/igpsport/history/confirm", response_class=HTMLResponse)
    async def confirm_igpsport_history_route(
        request: Request,
        start_date: str,
        end_date: str = "",
        max_activities: int = 20,
        import_action: str = "dry_run",
        _auth: None = Depends(require_auth),
    ) -> HTMLResponse:
        bounded_max = min(max(max_activities, 1), 500)
        mode = "upload" if import_action == "upload" else "dry_run"
        return templates.TemplateResponse(
            request,
            "igpsport_history.html",
            context(request)
            | {
                "start_date": start_date,
                "end_date": end_date,
                "max_activities": bounded_max,
                "import_action": mode,
            },
        )

    @app.post("/config/igpsport/history")
    async def import_igpsport_history_route(
        request: Request,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        form = await request.form()
        start_date = _form_text(form.get("start_date"), "")
        end_date = _form_text(form.get("end_date"), "")
        import_action = _form_text(form.get("import_action"), "dry_run")
        try:
            max_activities = int(_form_text(form.get("max_activities"), "20"))
        except ValueError:
            max_activities = 20
        if _form_text(form.get("confirm"), "") != "IMPORT":
            app.state.setup_message = {
                "ok": False,
                "title": "iGPSPORT historical import",
                "output": 'Enter "IMPORT" on the confirmation page to begin.',
            }
            return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)
        if import_action == "upload" and app.state.settings.dry_run:
            app.state.setup_message = {
                "ok": False,
                "title": "iGPSPORT historical import",
                "output": (
                    "Live historical upload was not started because global dry-run "
                    "mode is enabled."
                ),
            }
            return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)
        result = await asyncio.to_thread(
            app.state.source_manager.import_igpsport_history,
            start_date=start_date,
            end_date=end_date,
            max_activities=max_activities,
            dry_run=import_action != "upload",
        )
        scan_result = await asyncio.to_thread(app.state.service.scan_once)
        app.state.setup_message = {
            "ok": result.ok,
            "title": result.title,
            "output": (
                f"{result.message}\nDiscovered {scan_result['discovered']} file(s); "
                f"processed {scan_result['processed']} file(s)."
            ),
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/source/{source_type}/test")
    async def test_source_route(
        source_type: str,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        try:
            result = await asyncio.to_thread(
                app.state.source_manager.test_connection,
                source_type,
            )
            app.state.setup_message = {
                "ok": result.ok,
                "title": result.title,
                "output": result.message,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/source/{source_type}/sync")
    async def sync_source_route(
        source_type: str,
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        try:
            result = await asyncio.to_thread(
                app.state.source_manager.sync_source,
                source_type,
                manual=True,
            )
            scan_result = await asyncio.to_thread(app.state.service.scan_once)
            app.state.setup_message = {
                "ok": result.ok,
                "title": result.title,
                "output": (
                    f"{result.message}\nDiscovered {scan_result['discovered']} file(s); "
                    f"processed {scan_result['processed']} file(s)."
                ),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/sources/sync")
    async def sync_all_sources_route(
        _csrf: None = Depends(require_csrf),
    ) -> RedirectResponse:
        results = await asyncio.to_thread(
            app.state.source_manager.sync_all,
            manual=True,
        )
        scan_result = await asyncio.to_thread(app.state.service.scan_once)
        app.state.setup_message = {
            "ok": all(result.ok for result in results.values()),
            "title": "Activity source sync",
            "output": "\n".join(result.message for result in results.values())
            + (
                f"\nDiscovered {scan_result['discovered']} file(s); "
                f"processed {scan_result['processed']} file(s)."
            ),
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/dropbox/save")
    async def save_dropbox(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        runtime_settings = request.app.state.settings
        form = await request.form()
        remote_name = _form_text(
            form.get("rclone_remote"), runtime_settings.rclone_remote
        )
        dropbox_path = _form_text(
            form.get("dropbox_wahoo_path"), runtime_settings.dropbox_wahoo_path
        )
        result = save_dropbox_auth(
            runtime_settings,
            remote_name=remote_name,
            token_json=str(form.get("rclone_token_json", "")),
            full_config=str(form.get("rclone_config_text", "")),
        )
        if result.ok:
            save_runtime_config(
                runtime_settings,
                {
                    "RCLONE_REMOTE": remote_name,
                    "DROPBOX_WAHOO_PATH": dropbox_path,
                    "GARMIN_PROFILE_NAME": runtime_settings.garmin_profile_name,
                    "GARMIN_UNIT_ID": runtime_settings.garmin_unit_id,
                    "DRY_RUN": "true" if runtime_settings.dry_run else "false",
                },
            )
            updated_settings = replace(
                runtime_settings,
                rclone_remote=remote_name,
                dropbox_wahoo_path=dropbox_path,
            )
            updated_db = Database(updated_settings.sqlite_path)
            updated_service = BridgeService(updated_settings, updated_db)
            updated_service.setup()
            app.state.settings = updated_settings
            app.state.db = updated_db
            app.state.service = updated_service
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/garmin/save")
    async def save_garmin(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        runtime_settings = request.app.state.settings
        form = await request.form()
        profile_name = _form_text(
            form.get("garmin_profile_name"), runtime_settings.garmin_profile_name
        )
        unit_id = _form_text(
            form.get("garmin_unit_id"), runtime_settings.garmin_unit_id
        )
        selected_device = find_garmin_target(
            runtime_settings, str(form.get("detected_device_id", ""))
        )
        manufacturer_id = _form_text(form.get("garmin_manufacturer_id"), "1")
        product_id = str(form.get("garmin_product_id", ""))
        software_version = str(form.get("garmin_software_version", ""))
        if selected_device is not None:
            manufacturer_id = str(selected_device.manufacturer_id)
            product_id = str(selected_device.product_id)
            unit_id = str(selected_device.unit_id)
            software_version = str(selected_device.software_version or "")
            device_name = garmin_product_display_name(selected_device.garmin_product, selected_device.product_id)
        else:
            device_name = runtime_settings.garmin_device_name
        result = save_garmin_profile(
            runtime_settings,
            profile_name=profile_name,
            garmin_username=str(form.get("garmin_username", "")),
            garmin_password=str(form.get("garmin_password", "")),
            manufacturer=manufacturer_id,
            product_id=product_id,
            unit_id=unit_id,
            software_version=software_version,
        )
        if result.ok:
            save_runtime_config(
                runtime_settings,
                {
                    "RCLONE_REMOTE": runtime_settings.rclone_remote,
                    "DROPBOX_WAHOO_PATH": runtime_settings.dropbox_wahoo_path,
                    "GARMIN_PROFILE_NAME": profile_name,
                    "GARMIN_DEVICE_NAME": device_name,
                    "GARMIN_UNIT_ID": unit_id,
                    "DRY_RUN": "true" if runtime_settings.dry_run else "false",
                },
            )
            updated_settings = replace(
                runtime_settings,
                garmin_profile_name=profile_name,
                garmin_device_name=device_name,
                garmin_unit_id=unit_id,
            )
            updated_db = Database(updated_settings.sqlite_path)
            updated_service = BridgeService(updated_settings, updated_db)
            updated_service.setup()
            app.state.settings = updated_settings
            app.state.db = updated_db
            app.state.service = updated_service
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/garmin/clear-pause")
    async def clear_garmin_pause_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        result = clear_garmin_session_pause(app.state.settings)
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/garmin/session")
    async def create_garmin_session_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        result = create_garmin_session_token(app.state.settings)
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/garmin/upload-real-fit")
    async def upload_real_fit(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
        form = await request.form()
        upload = form.get("real_fit_file")
        if upload is None or not hasattr(upload, "file"):
            app.state.setup_message = {
                "ok": False,
                "title": "Garmin device scan",
                "output": "Choose a Garmin FIT file to upload.",
            }
            return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)
        saved_path = save_uploaded_real_fit(
            request.app.state.settings,
            getattr(upload, "filename", "garmin.fit"),
            upload.file,
        )
        devices = scan_real_fit_devices(request.app.state.settings)
        app.state.setup_message = {
            "ok": bool(devices),
            "title": "Garmin device scan",
            "output": f"Saved {saved_path.name} and found {len(devices)} Garmin device profile(s).",
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/garmin/scan-real-fit")
    async def scan_real_fit(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        runtime_settings = app.state.settings
        devices = await asyncio.to_thread(scan_real_fit_devices, runtime_settings)
        app.state.setup_message = {
            "ok": bool(devices),
            "title": "Garmin device scan",
            "output": (
                f"Found {len(devices)} Garmin device profile(s) in "
                f"{runtime_settings.real_fit_dir} and "
                f"{runtime_settings.real_fit_upload_dir}."
            ),
        }
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/test-dropbox")
    async def test_dropbox_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        result = await asyncio.to_thread(test_dropbox, app.state.settings)
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/config/test-garmin")
    async def test_garmin_route(_csrf: None = Depends(require_csrf)) -> RedirectResponse:
        result = await asyncio.to_thread(test_garmin_upload, app.state.settings)
        app.state.setup_message = result.__dict__
        return RedirectResponse("/config", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        status_info = get_system_status(request.app.state.settings, request.app.state.db)
        benchmark_results = await asyncio.to_thread(
            run_system_benchmark,
            request.app.state.settings,
            request.app.state.db,
        )
        return templates.TemplateResponse(
            request,
            "system.html",
            context(request) | {
                "status": status_info,
                "benchmark": benchmark_results,
                "update_status": getattr(request.app.state, "update_status", None),
            },
        )

    @app.api_route("/api/benchmark", methods=["GET", "POST"])
    async def run_benchmark(request: Request, _auth: None = Depends(require_auth)) -> JSONResponse:
        result = await asyncio.to_thread(
            run_system_benchmark,
            request.app.state.settings,
            request.app.state.db,
        )
        return JSONResponse(result)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "logs.html",
            context(request) | {"logs": _tail(request.app.state.settings.log_file)},
        )

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "help.html",
            context(request),
        )

    @app.get("/api/activities")
    async def api_activities(_auth: None = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(app.state.db.list_recent(100))

    @app.get("/api/stats")
    async def api_stats(_auth: None = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(app.state.db.stats())

    return app


async def _run_activity_action(app: FastAPI, service: BridgeService, activity_id: int, reset_retries: bool) -> None:
    title = "Reprocess" if reset_retries else "Retry"
    try:
        if reset_retries:
            existing = app.state.db.get_activity(activity_id)
            if existing is not None:
                source_type = str(existing.get("source_type") or "dropbox")
                if source_type in app.state.source_manager.sources:
                    await asyncio.to_thread(
                        app.state.source_manager.sync_source,
                        source_type,
                        manual=True,
                    )
        activity = await asyncio.to_thread(service.retry_now, activity_id, reset_retries)
    except (RuntimeError, sqlite3.OperationalError) as exc:
        _set_activity_message(app, activity_id, False, title, _friendly_action_error(exc))
        return
    status_value = activity.get("status", "unknown")
    message = f"{title} finished. Current status: {status_value}."
    if activity.get("error_message"):
        message += f"\n{activity['error_message']}"
    _set_activity_message(app, activity_id, status_value not in {"failed"}, title, message)


def _set_activity_message(app: FastAPI, activity_id: int, ok: bool, title: str, output: str) -> None:
    messages = getattr(app.state, "activity_messages", {})
    messages[activity_id] = {"ok": ok, "title": title, "output": output}
    app.state.activity_messages = messages


def _friendly_action_error(exc: Exception) -> str:
    text = str(exc)
    if "Another scan or upload is already running" in text or "database is locked" in text:
        return "Another scan or upload is already running. Wait a few seconds, then try again."
    return text or "The action could not be completed."


def _activity_for_display(activity: dict[str, object]) -> dict[str, object]:
    display = dict(activity)
    error_message = str(display.get("error_message") or "")
    if error_message:
        display["error_message"] = friendly_upload_error(error_message, 1)

    garmin_response = str(display.get("garmin_response") or "")
    if garmin_response:
        display["garmin_response"] = friendly_upload_error(garmin_response, 1)
    return display


def raise_auth(request: Request | None = None) -> None:
    if request is not None and request.method == "GET" and not request.url.path.startswith("/api/"):
        next_url = quote(str(request.url.path))
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Authentication required",
            headers={"Location": f"/login?next={next_url}"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def _tail(path: Path, lines: int = 300) -> str:
    if not path.exists():
        return "No logs yet."
    content = path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _form_text(value: object, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def group_activities_by_month(activities: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for activity in activities:
        dt_str = str(activity.get("activity_start_time") or activity.get("first_seen_at") or "")
        month_label = "Unknown Date"
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                month_label = dt.strftime("%B %Y")
            except Exception:
                month_label = "Unknown Date"
        if month_label not in groups:
            groups[month_label] = []
        groups[month_label].append(activity)
    return list(groups.items())


def _safe_next(value: str) -> str:
    if value in {"/", "/config", "/logs"}:
        return value

    activity_prefix = "/activity/"
    if value.startswith(activity_prefix):
        activity_id = value.removeprefix(activity_prefix)
        if activity_id.isdecimal():
            return f"{activity_prefix}{activity_id}"

    return "/"


async def _update_check_loop(app_instance: FastAPI) -> None:
    """Periodically check for newer stable GitHub releases."""
    import logging

    from app.update_checker import CACHE_TTL_SECONDS

    logger = logging.getLogger(__name__)
    await asyncio.sleep(30)
    while True:
        try:
            current = app_instance.state.app_version
            result = await asyncio.to_thread(check_for_update, current)
            app_instance.state.update_status = result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Update check failed: %s", exc)
        await asyncio.sleep(CACHE_TTL_SECONDS)


app = create_app()
