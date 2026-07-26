from __future__ import annotations

from app.security import RateLimiter, make_password_hash, sign_session, verify_password, verify_session
from app.setup_status import _quote_env


def test_password_hash_round_trip():
    encoded = make_password_hash("secret")

    assert verify_password("secret", "", encoded)
    assert not verify_password("wrong", "", encoded)


def test_signed_session_round_trip():
    cookie = sign_session("admin", "secret-key", 60)
    session = verify_session(cookie, "secret-key")

    assert session is not None
    assert session.username == "admin"
    assert verify_session(cookie, "wrong-key") is None


def test_rate_limiter_prunes_expired_keys(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.security.time.time", lambda: now[0])
    limiter = RateLimiter(attempts=3, window_seconds=10)
    limiter.record_failure("expired")

    now[0] = 111.0
    limiter.record_failure("current")

    assert "expired" not in limiter._failures
    assert "current" in limiter._failures


def test_quote_env_quotes_whitespace_and_shell_special_characters():
    assert _quote_env("plain") == "plain"
    assert _quote_env("two words") == "'two words'"
    assert _quote_env("value#comment") == "'value#comment'"
