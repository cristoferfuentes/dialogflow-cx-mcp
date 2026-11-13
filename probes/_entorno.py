"""Configuracion compartida por las sondas: **nada cableado**.

Los ids de agente que usan las sondas se toman del entorno:

    CXP_PROBE_AGENTS="original=uuid1,uuid2;clon=uuid3,uuid4"

Si no se pone nada, se usa el agente por defecto del `.env`
(`CXP_DEFAULT_AGENT`), bajo la clave `por_defecto`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config  # noqa: E402


def agentes_de_sonda() -> dict[str, tuple[str, ...]]:
    """Los agentes que usa la sonda, por nombre.

    Formato: `CXP_PROBE_AGENTS="original=uuid1,uuid2;clon=uuid3"`.
    """
    out: dict[str, tuple[str, ...]] = {}
    for parte in os.environ.get("CXP_PROBE_AGENTS", "").split(";"):
        if "=" not in parte:
            continue
        clave, _, valores = parte.partition("=")
        ids = tuple(v.strip() for v in valores.split(",") if v.strip())
        if ids:
            out[clave.strip()] = ids
    if not out:
        cfg = load_config()
        if cfg.default_agent:
            out["por_defecto"] = (cfg.default_agent,)
    return out


def agente(nombre: str = "por_defecto") -> str:
    """El primer id de un grupo de agentes de la sonda (o el del `.env`)."""
    grupos = agentes_de_sonda()
    ids = grupos.get(nombre) or next(iter(grupos.values()), ())
    return ids[0] if ids else ""


def targets_de_sonda() -> dict[str, dict[str, str]]:
    """Los (agente, flow, pagina) que usan las sondas, por nombre.

    Formato: `CXP_PROBE_TARGETS="clon=agente,flow,pagina;otro=agente,flow,pagina"`.
    """
    out: dict[str, dict[str, str]] = {}
    for parte in os.environ.get("CXP_PROBE_TARGETS", "").split(";"):
        if "=" not in parte:
            continue
        clave, _, valores = parte.partition("=")
        ids = [v.strip() for v in valores.split(",") if v.strip()]
        if len(ids) >= 3:
            out[clave.strip()] = {"agent": ids[0], "flow": ids[1], "page": ids[2]}
    return out
