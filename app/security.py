from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthSession:
    username: str
    expires_at: int
    nonce: str


class RateLimiter:
    def __init__(self, attempts: int, window_seconds: int) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._last_prune = 0.0

    def is_limited(self, key: str) -> bool:
        now = time.time()
        self._prune(now)
        failures = [ts for ts in self._failures.get(key, []) if now - ts <= self.window_seconds]
        if failures:
            self._failures[key] = failures
        else:
            self._failures.pop(key, None)
        return len(failures) >= self.attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        self._prune(now)
        failures = [ts for ts in self._failures.get(key, []) if now - ts <= self.window_seconds]
        failures.append(now)
        self._failures[key] = failures

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def _prune(self, now: float) -> None:
        if now - self._last_prune < self.window_seconds:
            return
        cutoff = now - self.window_seconds
        for key, failures in list(self._failures.items()):
            recent = [timestamp for timestamp in failures if timestamp >= cutoff]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)
        self._last_prune = now


def verify_password(candidate: str, password: str, password_hash: str = "") -> bool:
    if password_hash:
        return _verify_pbkdf2(candidate, password_hash)
    return hmac.compare_digest(candidate, password)


def make_password_hash(password: str, iterations: int = 260_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        _b64(salt),
        _b64(digest),
    )


def sign_session(username: str, secret: str, max_age_seconds: int) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + max_age_seconds,
        "nonce": _b64(os.urandom(24)),
    }
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _sign(payload_b64, secret)
    return f"{payload_b64}.{signature}"


def verify_session(cookie_value: str, secret: str) -> AuthSession | None:
    try:
        payload_b64, signature = cookie_value.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload_b64, secret), signature):
        return None
    try:
        payload = json.loads(_unb64(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    expires_at = int(payload.get("exp", 0))
    if expires_at < int(time.time()):
        return None
    username = str(payload.get("sub", ""))
    nonce = str(payload.get("nonce", ""))
    if not username or not nonce:
        return None
    return AuthSession(username=username, expires_at=expires_at, nonce=nonce)


def csrf_token(session_cookie: str, secret: str) -> str:
    return _sign(f"csrf:{session_cookie}", secret)


def verify_csrf(session_cookie: str, token: str, secret: str) -> bool:
    if not session_cookie or not token:
        return False
    return hmac.compare_digest(csrf_token(session_cookie, secret), token)


def _verify_pbkdf2(candidate: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations_int = int(iterations)
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", candidate.encode(), salt, iterations_int)
    return hmac.compare_digest(actual, expected)


def _sign(value: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), value.encode(), hashlib.sha256).digest()
    return _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
