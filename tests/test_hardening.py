from __future__ import annotations

import io
from dataclasses import replace

import pytest

from app.garmin_device import save_uploaded_real_fit
from app.redaction import redact_sensitive_text


def test_placeholder_credentials_are_rejected(settings):
    insecure = replace(
        settings,
        web_auth_enabled=True,
        web_password="change-this-password",
        session_secret_key="change-this-long-random-secret",
    )

    with pytest.raises(ValueError, match="WEB_PASSWORD"):
        insecure.validate_security()


def test_real_fit_upload_size_is_limited(settings):
    limited = replace(settings, max_real_fit_upload_bytes=4)

    with pytest.raises(ValueError, match="exceeds"):
        save_uploaded_real_fit(limited, "watch.fit", io.BytesIO(b"12345"))

    assert not (limited.real_fit_upload_dir / "watch.fit").exists()


def test_sensitive_values_are_redacted():
    text = (
        'password="secret" access_token: abc123 '
        "Authorization: Bearer header.payload.signature known-value"
    )

    redacted = redact_sensitive_text(text, ("known-value",))

    assert "secret" not in redacted
    assert "abc123" not in redacted
    assert "header.payload.signature" not in redacted
    assert "known-value" not in redacted
