#!/usr/bin/env python3
"""Helpers to write files that may carry credentials.

The probes of this repository work with the **live session cookies** of the
browser. Several of them dump what they captured (headers, requests) to a file
in /tmp. Two rules apply to every one of those files:

1. the credential values are **redacted** before they are written
   (`cookie`, `authorization`, ...);
2. the file is created with mode **0600**: only its owner can read it.

Rule 1 does not apply to the files whose whole purpose is to carry the
credentials (the header file that is fed to `curl`): those are only written
with mode 0600.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

#: Headers whose value is a credential.
SECRET_HEADERS = (
    "cookie",
    "authorization",
    "x-goog-api-key",
    "proxy-authorization",
    "sapisisid",
    "x-apikey",
)

#: Keys of a captured request that hold the credentials.
SECRET_KEYS = (
    "cookie",
    "authorization",
    "sapisisid",
    "cookies",
    "session_state",
)

REDACTED = "<redacted>"


def is_secret(key: str) -> bool:
    """Whether a header/key name holds a credential."""
    k = (key or "").lower()
    return k in SECRET_HEADERS or k in SECRET_KEYS


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Copy of a headers dict with the credential values replaced."""
    return {k: (REDACTED if is_secret(k) else v) for k, v in headers.items()}


def redact(obj: Any) -> Any:
    """Recursive redaction of every credential found in a JSON-ish structure."""
    if isinstance(obj, dict):
        return {k: (REDACTED if is_secret(k) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def write_private(dest: str | Path, text: str) -> Path:
    """Write a file that only its owner can read (0600)."""
    p = Path(dest)
    p.write_text(text, encoding="utf-8")
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return p


def write_json_private(dest: str | Path, obj: Any,
                       *, redact_secrets: bool = True) -> Path:
    """Write a JSON file (0600), redacting the credentials by default."""
    data = redact(obj) if redact_secrets else obj
    return write_private(dest, json.dumps(data, indent=1, ensure_ascii=False))


def ensure_private(dest: str | Path) -> None:
    """Make an existing file readable only by its owner."""
    try:
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
