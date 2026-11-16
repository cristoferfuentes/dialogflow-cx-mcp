"""Shared configuration for the probes: **nothing hardwired**.

The agent ids used by the probes are taken from the environment:

    CXP_PROBE_AGENTS="original=uuid1,uuid2;clon=uuid3,uuid4"

If nothing is set, the default agent from the `.env`
(`CXP_DEFAULT_AGENT`) is used, under the `por_defecto` key.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config  # noqa: E402


def agentes_de_sonda() -> dict[str, tuple[str, ...]]:
    """The agents used by the probe, by name.

    Format: `CXP_PROBE_AGENTS="original=uuid1,uuid2;clon=uuid3"`.
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
    """The first id of a probe agent group (or the one from the `.env`)."""
    grupos = agentes_de_sonda()
    ids = grupos.get(nombre) or next(iter(grupos.values()), ())
    return ids[0] if ids else ""


def targets_de_sonda() -> dict[str, dict[str, str]]:
    """The (agent, flow, page) triples used by the probes, by name.

    Format: `CXP_PROBE_TARGETS="clon=agent,flow,page;otro=agent,flow,page"`.
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
