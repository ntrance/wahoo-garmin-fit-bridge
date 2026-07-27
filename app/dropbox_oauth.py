from __future__ import annotations

import re
# Commands use fixed argv and never enable shell execution.
import subprocess  # nosec B404
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

from app.setup_status import CommandResult, save_dropbox_auth
from app.settings import Settings


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass
class DropboxOAuthSession:
    id: str
    process: subprocess.Popen[str]
    local_auth_url: str
    authorization_url: str
    started_at: float


def start_dropbox_oauth(settings: Settings) -> tuple[DropboxOAuthSession | None, CommandResult]:
    command = ["rclone", "authorize", "dropbox", "--auth-no-open-browser"]
    try:
        process = subprocess.Popen(  # nosec B603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return None, CommandResult(False, "Dropbox OAuth", f"Could not start rclone authorize: {exc}")

    lines: list[str] = []
    local_auth_url = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.stdout is None:
            break
        line = process.stdout.readline()
        if line:
            lines.append(line)
            match = re.search(r"http://127\.0\.0\.1:53682/auth\?state=[^\s]+", line)
            if match:
                local_auth_url = match.group(0)
                break
        elif process.poll() is not None:
            break

    if not local_auth_url:
        process.terminate()
        return None, CommandResult(
            False,
            "Dropbox OAuth",
            "Could not get an rclone authorization URL.\n" + "".join(lines),
        )

    authorization_url = _dropbox_authorization_url(local_auth_url)
    if not authorization_url:
        process.terminate()
        return None, CommandResult(
            False,
            "Dropbox OAuth",
            "Could not translate rclone's local auth URL into a Dropbox authorization URL.",
        )

    session = DropboxOAuthSession(
        id=uuid.uuid4().hex,
        process=process,
        local_auth_url=local_auth_url,
        authorization_url=authorization_url,
        started_at=time.time(),
    )
    return session, CommandResult(True, "Dropbox OAuth", "Dropbox authorization started.")


def complete_dropbox_oauth(
    settings: Settings,
    session: DropboxOAuthSession,
    callback_url: str,
    remote_name: str,
    dropbox_path: str,
) -> CommandResult:
    parsed = urllib.parse.urlparse(callback_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1"}:
        return CommandResult(False, "Dropbox OAuth", "Paste the final localhost callback URL from Dropbox.")
    if not parsed.query:
        return CommandResult(False, "Dropbox OAuth", "The callback URL does not contain an OAuth code.")

    local_callback = urllib.parse.urlunparse(
        ("http", "127.0.0.1:53682", parsed.path or "/", "", parsed.query, "")
    )
    try:
        # local_callback is reconstructed as fixed loopback HTTP above.
        urllib.request.urlopen(  # nosec B310
            local_callback,
            timeout=10,
        ).read()
    except urllib.error.HTTPError:
        pass
    except OSError as exc:
        return CommandResult(False, "Dropbox OAuth", f"Could not send callback to rclone: {exc}")

    try:
        output, _ = session.process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        session.process.terminate()
        return CommandResult(False, "Dropbox OAuth", "rclone did not finish after receiving the callback URL.")

    token_json = extract_rclone_token(output)
    if not token_json:
        return CommandResult(False, "Dropbox OAuth", "Could not find a Dropbox token in rclone output.\n" + output[-2000:])

    save_result = save_dropbox_auth(
        settings,
        remote_name=remote_name,
        token_json=token_json,
    )
    if not save_result.ok:
        return save_result

    return CommandResult(
        True,
        "Dropbox OAuth",
        f"Dropbox is connected as [{remote_name}] and mapped to {dropbox_path}.",
    )


def extract_rclone_token(output: str) -> str:
    match = re.search(r"(\{[^\n]*\"access_token\"[^\n]*\})", output)
    return match.group(1) if match else ""


def _dropbox_authorization_url(local_auth_url: str) -> str:
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(local_auth_url, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            return exc.headers.get("Location", "")
    except OSError:
        return ""
    return ""
