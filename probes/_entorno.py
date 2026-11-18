"""Shared configuration for the probes: **nothing hardwired**.

The agent ids used by the probes are taken from the environment:

    CXP_PROBE_AGENTS="original=uuid1,uuid2;clone=uuid3,uuid4"

If nothing is set, the **only** agent of the allowlist (`.env`) is used, under
the `default` key. With more than one agent the probe says so: it does not
guess.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config  # noqa: E402

#: Legacy name of the default group (kept: an existing `CXP_PROBE_*` value
#: may still use it).
LEGACY_DEFAULT = "por_defecto"


def probe_agents() -> dict[str, tuple[str, ...]]:
    """The agents used by the probe, by name.

    Format: `CXP_PROBE_AGENTS="original=uuid1,uuid2;clone=uuid3"`.
    """
    out: dict[str, tuple[str, ...]] = {}
    for part in os.environ.get("CXP_PROBE_AGENTS", "").split(";"):
        if "=" not in part:
            continue
        key, _, values = part.partition("=")
        ids = tuple(v.strip() for v in values.split(",") if v.strip())
        if ids:
            out[key.strip()] = ids
    if not out:
        cfg = load_config()
        if cfg.only_agent():
            out["default"] = (cfg.only_agent(),)
    return out


def agent(name: str = "default") -> str:
    """The first id of a probe agent group (or the one from the `.env`)."""
    groups = probe_agents()
    ids = (groups.get(name) or groups.get(LEGACY_DEFAULT)
           or next(iter(groups.values()), ()))
    return ids[0] if ids else ""


def probe_targets() -> dict[str, dict[str, str]]:
    """The (agent, flow, page) triples used by the probes, by name.

    Format: `CXP_PROBE_TARGETS="clone=agent,flow,page;other=agent,flow,page"`.
    """
    out: dict[str, dict[str, str]] = {}
    for part in os.environ.get("CXP_PROBE_TARGETS", "").split(";"):
        if "=" not in part:
            continue
        key, _, values = part.partition("=")
        ids = [v.strip() for v in values.split(",") if v.strip()]
        if len(ids) >= 3:
            out[key.strip()] = {"agent": ids[0], "flow": ids[1], "page": ids[2]}
    return out
