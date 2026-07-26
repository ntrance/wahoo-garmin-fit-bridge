from __future__ import annotations

import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import Database
from app.dropbox_oauth import complete_dropbox_oauth, start_dropbox_oauth
from app.garmin_upload import friendly_upload_error
from app.fit_preview import build_activity_preview
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
    sync_dropbox_to_incoming,
    test_dropbox,
    test_garmin_upload,
)
from app.setup_status import save_dropbox_auth, save_garmin_profile
from app.settings import Settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    db = Database(settings.sqlite_path)
    service = BridgeService(settings, db)
    rate_limiter = RateLimiter(
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )
    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        runtime_settings = app_instance.state.settings
        runtime_service = app_instance.state.service
        runtime_settings.validate_security()
        runtime_service.setup()
        if start_background:
            app_instance.state.scan_task = asyncio.create_task(runtime_service.run_forever())
        try:
            yield
        finally:
            task = getattr(app_instance.state, "scan_task", None)
            if task is not None:
                task.cancel()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.state.settings = settings
    app.state.db = db
    app.state.service = service

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        runtime_settings = request.app.state.settings
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://tile.openstreetmap.org; "
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
    async def index(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        runtime_db = request.app.state.db
        stats = runtime_db.stats()
        activities = runtime_db.list_recent(50)
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
                "cleanup_activities": cleanup_activities,
                "dashboard_message": dashboard_message,
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
        preview = await asyncio.to_thread(build_activity_preview, activity)
        return templates.TemplateResponse(
            request,
            "activity.html",
            context(request)
            | {
                "activity": _activity_for_display(activity),
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
            runtime_settings = app.state.settings
            runtime_service = app.state.service
            sync_result = await asyncio.to_thread(
                sync_dropbox_to_incoming, runtime_settings
            )
            scan_result = {"discovered": 0, "processed": 0}
            if sync_result.ok:
                scan_result = await asyncio.to_thread(runtime_service.scan_once)
            app.state.dashboard_message = {
                "ok": sync_result.ok,
                "title": "Rescan",
                "output": (
                    f"{sync_result.output}\n"
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
        }
        save_runtime_config(runtime_settings, updates)
        updated_settings = replace(
            runtime_settings,
            rclone_remote=updates["RCLONE_REMOTE"],
            dropbox_wahoo_path=updates["DROPBOX_WAHOO_PATH"],
            garmin_profile_name=updates["GARMIN_PROFILE_NAME"],
            garmin_unit_id=updates["GARMIN_UNIT_ID"],
            dry_run=updates["DRY_RUN"] == "true",
        )
        updated_db = Database(updated_settings.sqlite_path)
        updated_service = BridgeService(updated_settings, updated_db)
        updated_service.setup()
        app.state.settings = updated_settings
        app.state.db = updated_db
        app.state.service = updated_service
        app.state.setup_message = {
            "ok": True,
            "title": "Settings saved",
            "output": "Saved to runtime config. Restart the bridge container so the background scanner uses the new values.",
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

    @app.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request, _auth: None = Depends(require_auth)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "logs.html",
            context(request) | {"logs": _tail(request.app.state.settings.log_file)},
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
            await asyncio.to_thread(sync_dropbox_to_incoming, service.settings)
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


def _safe_next(value: str) -> str:
    if value in {"/", "/config", "/logs"}:
        return value

    activity_prefix = "/activity/"
    if value.startswith(activity_prefix):
        activity_id = value.removeprefix(activity_prefix)
        if activity_id.isdecimal():
            return f"{activity_prefix}{activity_id}"

    return "/"


app = create_app()
