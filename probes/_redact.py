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
CABECERAS_SECRETAS = (
    "cookie",
    "authorization",
    "x-goog-api-key",
    "proxy-authorization",
    "sapisisid",
    "x-apikey",
)

#: Keys of a captured request that hold the credentials.
CLAVES_SECRETAS = (
    "cookie",
    "authorization",
    "sapisisid",
    "cookies",
    "session_state",
)

REDACTADO = "<redacted>"


def es_secreto(clave: str) -> bool:
    """Whether a header/key name holds a credential."""
    k = (clave or "").lower()
    return k in CABECERAS_SECRETAS or k in CLAVES_SECRETAS


def redacta_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Copy of a headers dict with the credential values replaced."""
    return {k: (REDACTADO if es_secreto(k) else v) for k, v in headers.items()}


def redacta(objeto: Any) -> Any:
    """Recursive redaction of every credential found in a JSON-ish structure."""
    if isinstance(objeto, dict):
        return {k: (REDACTADO if es_secreto(k) else redacta(v))
                for k, v in objeto.items()}
    if isinstance(objeto, list):
        return [redacta(v) for v in objeto]
    return objeto


def escribe_privado(destino: str | Path, texto: str) -> Path:
    """Write a file that only its owner can read (0600)."""
    p = Path(destino)
    p.write_text(texto, encoding="utf-8")
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return p


def escribe_json_privado(destino: str | Path, objeto: Any,
                         *, redactar: bool = True) -> Path:
    """Write a JSON file (0600), redacting the credentials by default."""
    datos = redacta(objeto) if redactar else objeto
    return escribe_privado(destino, json.dumps(datos, indent=1, ensure_ascii=False))


def asegura_privado(destino: str | Path) -> None:
    """Make an existing file readable only by its owner."""
    try:
        os.chmod(destino, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
