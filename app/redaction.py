from __future__ import annotations

import re

_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?
        (?:access_token|refresh_token|id_token|token|password|client_secret)
        ["']?
        \s*[:=]\s*
        ["']?
    )
    (?P<value>[^"',\s}]+)
    """
)
_BEARER_TOKEN = re.compile(
    r"(?i)(?P<prefix>authorization\s*[:=]\s*bearer\s+)(?P<value>[A-Za-z0-9._~+/=-]+)"
)


def redact_sensitive_text(text: str, known_secrets: tuple[str, ...] = ()) -> str:
    redacted = text or ""
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = _SECRET_ASSIGNMENT.sub(r"\g<prefix>[redacted]", redacted)
    return _BEARER_TOKEN.sub(r"\g<prefix>[redacted]", redacted)
