#!/usr/bin/env python3
"""Downloads the CONVERSATION LOGS of Dialogflow CX **through the API** (without BigQuery).

WHERE THE LOGS ARE (measured 2026-11-16, not assumed)
---------------------------------------------------------
The resource that stores the conversation history of a CX agent is
`agents.conversations` (NOT `agents.sessions`):

    GET {backend}/{version}/projects/{p}/locations/{l}/agents/{a}/conversations/{c}

* `agents.sessions` is ONLY the live state of a session (params, entity types):
  it does not store the history.
* `agents.conversations` stores the log: `interactions[]` with
  `request` (queryInput + queryParams) and `response` (queryResult with the agent
  text, `match`, `generativeInfo` and `stepMetrics` with the latencies).

The ids the user gives (`176789778-1794838739362-58e90084de99ffa8`) have the
shape `<prefix>-<epoch_ms>-<hash>`: they are **`conversations`**, not CX UUIDs.

The backend used is the CONSOLE one (`dialogflow.clients6.google.com`,
first party authentication with `SAPISIDHASH` + cookies), because it is the one
that serves the full history: the public `get` (`dialogflow.googleapis.com`)
returns the conversation WITHOUT `interactions` (metadata only).

USAGE
---
    .venv/bin/python probes/exp_logs_conversaciones.py \
        --agente 22222222-2222-4222-8222-222222222222 \
        --proyecto my-project \
        --session 176789778-1794838739362-58e90084de99ffa8 ... \
        --out ./logs-conversaciones
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cxp.config import load_config
from cxp.console_api import ConsoleApi


# Extracts the agent text from a list of `responseMessages`.
def texto_agente(mensajes: Any) -> str:
    if not isinstance(mensajes, list):
        return ""
    partes: list[str] = []
    for m in mensajes:
        if not isinstance(m, dict):
            continue
        t = m.get("text")
        if isinstance(t, dict):
            for txt in (t.get("text") or []):
                if isinstance(txt, str) and txt.strip():
                    partes.append(txt)
    return "\n\n".join(partes)


def texto_usuario(qi: Any) -> str:
    """The user text comes from `request.queryInput` (several possible places)."""
    if not isinstance(qi, dict):
        return ""
    t = qi.get("text")
    if isinstance(t, dict):
        v = t.get("text")
        if isinstance(v, str):
            return v
        if isinstance(v, list):            # sometimes it is a list
            return "\n".join(str(x) for x in v)
    if isinstance(qi.get("event"), dict):
        return f"[event] {qi['event'].get('name', '')}"
    return ""


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:80]


def payloads_agente(mensajes: Any) -> list[dict[str, Any]]:
    """The `responseMessages` with a `payload` (buttons, carousel, greeting...)."""
    if not isinstance(mensajes, list):
        return []
    out: list[dict[str, Any]] = []
    for m in mensajes:
        if isinstance(m, dict) and m.get("payload"):
            out.append({"responseType": m.get("responseType"),
                        "payload": m["payload"]})
    return out


def resumen_traza(trace_blocks: Any) -> list[dict[str, Any]]:
    """Summary of `traceBlocks`: which flow/page/intent were traversed."""
    if not isinstance(trace_blocks, list):
        return []
    out: list[dict[str, Any]] = []
    for tb in trace_blocks:
        if not isinstance(tb, dict):
            continue
        meta = tb.get("flowTraceMetadata") or {}
        fila: dict[str, Any] = {
            "flow": (meta.get("displayName") or ""),
            "flowId": (meta.get("flow") or "").split("/")[-1],
        }
        pasos: list[str] = []
        for acc in (tb.get("actions") or []):
            if not isinstance(acc, dict):
                continue
            etiqueta = acc.get("displayName")
            extra: dict[str, Any] = {}
            im = acc.get("intentMatch")
            if isinstance(im, dict):
                extra["intents"] = [
                    {"displayName": (i or {}).get("displayName"),
                     "score": (i or {}).get("score")}
                    for i in (im.get("matchedIntents") or [])]
            fsu = acc.get("flowStateUpdate")
            if isinstance(fsu, dict):
                extra["eventType"] = fsu.get("eventType")
                ps = fsu.get("pageState") or {}
                extra["page"] = (ps.get("page") or "").split("/")[-1]
            if acc.get("userUtterance"):
                extra["userUtterance"] = (acc["userUtterance"] or {}).get("text")
            if acc.get("agentUtterance"):
                extra["agentUtterance"] = (acc["agentUtterance"] or {}).get("text")
            pasos.append({"paso": etiqueta,
                          "startTime": acc.get("startTime"),
                          "completeTime": acc.get("completeTime"),
                          **extra})
        if pasos:
            fila["pasos"] = pasos
        out.append(fila)
    return out


def linea_turno(idx: int, it: dict) -> dict[str, Any]:
    req = it.get("request") or {}
    resp = it.get("response") or {}
    qr = resp.get("queryResult") or {}
    match = qr.get("match") or {}
    intent = match.get("intent") or {}
    gen = qr.get("generativeInfo") or {}
    params = qr.get("parameters") or {}
    lat = {m.get("name"): m.get("latency")
           for m in (it.get("stepMetrics") or []) if isinstance(m, dict)}
    return {
        "n": idx,
        "createTime": it.get("createTime"),
        "usuario": texto_usuario(req.get("queryInput")),
        "agente": texto_agente(qr.get("responseMessages")) or (qr.get("text") or ""),
        "payloads": payloads_agente(qr.get("responseMessages")),
        "matchType": match.get("matchType"),
        "intent": intent.get("displayName"),
        "confidence": match.get("confidence"),
        "intentDetectionConfidence": qr.get("intentDetectionConfidence"),
        "playbooks": [pb.split("/")[-1] for pb in
                      (gen.get("currentPlaybooks") or []) if pb],
        "parameters": {k: v for k, v in params.items()
                       if isinstance(v, (str, int, float, bool))},
        "latencias": lat,
        "traza": resumen_traza(qr.get("traceBlocks")),
        "missingTransition": it.get("missingTransition"),
        "answerFeedback": it.get("answerFeedback") or {},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agente", default="")
    ap.add_argument("--proyecto", default="")
    ap.add_argument("--location", default="")
    ap.add_argument("--session", action="append", default=[],
                    help="conversation ID (repeatable)")
    ap.add_argument("--out", default=os.environ.get(
        "CXP_LOGS_DIR", str(RAIZ / "logs-conversaciones")))
    args = ap.parse_args()

    cfg = load_config()
    proyecto = args.proyecto or cfg.project
    location = args.location or cfg.location
    agente = args.agente or cfg.default_agent
    cons = ConsoleApi(cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    base = f"/projects/{proyecto}/locations/{location}/agents/{agente}"
    resumen: list[dict[str, Any]] = []

    for sid in args.session:
        reg: dict[str, Any] = {"session_id": sid, "agente": agente,
                               "proyecto": proyecto, "location": location}
        try:
            conv = cons.request("GET", cons._url(f"{base}/conversations/{sid}"))
        except Exception as exc:
            reg["estado"] = "error"
            reg["detalle"] = str(exc)[:300]
            print(f"ERROR {sid}: {str(exc)[:160]}")
            resumen.append(reg)
            continue

        reg["estado"] = "ok"
        reg["startTime"] = conv.get("startTime")
        reg["duration"] = conv.get("duration")
        reg["type"] = conv.get("type")
        reg["languageCode"] = conv.get("languageCode")
        reg["metrics"] = conv.get("metrics")
        reg["flows"] = [f.get("name", "").split("/")[-1] for f in (conv.get("flows") or [])]
        interacciones = conv.get("interactions") or []
        reg["turnos"] = [linea_turno(i, it) for i, it in enumerate(interacciones)]
        reg["interacciones_brutas"] = len(interacciones)

        # (1) full raw JSON
        (out / f"{_safe(sid)}.json").write_text(
            json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")

        # (2) readable Markdown
        md: list[str] = []
        md.append(f"# Conversation {sid}\n")
        md.append(f"- **Agent**: `{agente}` (project `{proyecto}`, "
                  f"location `{location}`)")
        md.append(f"- **Start**: {reg['startTime']}")
        md.append(f"- **Duration**: {reg['duration']}  |  **Type**: {reg['type']}  "
                  f"|  **Language**: {reg['languageCode']}")
        if reg.get("flows"):
            md.append(f"- **Flows**: {', '.join(reg['flows'])}")
        m = reg.get("metrics") or {}
        if m:
            md.append(f"- **Metrics**: interactions={m.get('interactionCount')}, "
                      f"average confidence={m.get('averageMatchConfidence')}, "
                      f"max webhook latency={m.get('maxWebhookLatency')}")
            qi = m.get("queryInputCount") or {}
            if qi:
                md.append(f"  - By input type: {json.dumps(qi, ensure_ascii=False)}")
            mt = m.get("matchTypeCount") or {}
            if mt:
                md.append(f"  - By match type: {json.dumps(mt, ensure_ascii=False)}")
        md.append("\n---\n")

        for t in reg["turnos"]:
            md.append(f"## Turn {t['n']}  ({t.get('createTime')})\n")
            md.append(f"**User**: {t['usuario'] or '_(no text)_'}\n")
            md.append(f"**Agent**: {t['agente'] or '_(no text response)_'}\n")
            md.append(f"- Match: `{t.get('matchType')}` | Intent: "
                      f"`{t.get('intent')}` | confidence: {t.get('confidence')}")
            if t.get("playbooks"):
                md.append(f"- Playbooks: {', '.join(t['playbooks'])}")
            if t.get("latencias"):
                md.append("- Latencies: "
                          + ", ".join(f"{k}={v}" for k, v in t["latencias"].items()))
            if t.get("missingTransition"):
                md.append(f"- **missingTransition**: "
                          f"`{json.dumps(t['missingTransition'], ensure_ascii=False)[:300]}`")
            if t.get("answerFeedback"):
                md.append(f"- answerFeedback: "
                          f"`{json.dumps(t['answerFeedback'], ensure_ascii=False)[:200]}`")
            md.append("")

            if t.get("payloads"):
                md.append("### Response payloads\n")
                for pl in t["payloads"]:
                    md.append(f"- **{pl.get('responseType')}**: "
                              f"`{json.dumps(pl['payload'], ensure_ascii=False)}`")
                md.append("")

            if t.get("traza"):
                md.append("### Conversation trace\n")
                for fb in t["traza"]:
                    md.append(f"**Flow**: {fb.get('flow')} (`{fb.get('flowId')}`)\n")
                    for p in fb.get("pasos", []):
                        md.append(f"- `{p.get('paso')}`")
                        if p.get("eventType"):
                            md.append(f" — event `{p['eventType']}`")
                        if p.get("page"):
                            md.append(f" — page `{p['page']}`")
                        if p.get("intents"):
                            md.append(" — intents: " + ", ".join(
                                f"{i.get('displayName')}({i.get('score')})"
                                for i in p["intents"]))
                        if p.get("userUtterance"):
                            md.append(f"\n  - user: {p['userUtterance']}")
                        if p.get("agentUtterance"):
                            md.append(f"\n  - agent: {str(p['agentUtterance'])[:400]}")
                        md.append("")
                md.append("")

            if t.get("parameters"):
                md.append("### Session parameters\n")
                md.append("```json")
                md.append(json.dumps(t["parameters"], ensure_ascii=False, indent=2)[:4000])
                md.append("```\n")
        (out / f"{_safe(sid)}.md").write_text("\n".join(md), encoding="utf-8")

        print(f"OK {sid}: {len(interacciones)} interactions -> "
              f"{_safe(sid)}.json / .md")
        resumen.append(reg)

    (out / "resumen.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")

    # Readable index of all the conversations
    idx_md: list[str] = ["# Conversation logs — Dialogflow CX (via API)\n"]
    idx_md.append(f"- **Agent**: `{agente}`  |  **Project**: `{proyecto}`  |  "
                  f"**Location**: `{location}`")
    idx_md.append(f"- **Origin**: `GET {cons.cfg.console_backend}/"
                  f"{cons.cfg.api_version}/projects/.../agents/.../conversations/{{id}}` "
                  f"(console backend; no BigQuery)")
    idx_md.append(f"- **Generated**: {datetime.now(timezone.utc).isoformat()}")
    idx_md.append("| # | Session ID | Start | Duration | Turns | Avg. confidence | File |")
    idx_md.append("|---|---|---|---|---|---|---|")
    for n, r in enumerate(resumen, 1):
        if r.get("estado") != "ok":
            idx_md.append(f"| {n} | `{r['session_id']}` | - | - | ERROR | - | - |")
            continue
        m = r.get("metrics") or {}
        idx_md.append(
            f"| {n} | `{r['session_id']}` | {r.get('startTime')} | {r.get('duration')} | "
            f"{r.get('interacciones_brutas')} | {m.get('averageMatchConfidence')} | "
            f"[md]({_safe(r['session_id'])}.md) / [json]({_safe(r['session_id'])}.json) |")
    idx_md.append("\n---\n")
    for n, r in enumerate(resumen, 1):
        if r.get("estado") != "ok":
            continue
        idx_md.append(f"## {n}. `{r['session_id']}`\n")
        for t in r.get("turnos", []):
            u = (t.get("usuario") or "").replace("\n", " ")[:160]
            a = (t.get("agente") or "").replace("\n", " ")[:220]
            idx_md.append(f"- **Turn {t['n']}** — user: *{u}*")
            idx_md.append(f"  - agent: {a}")
            idx_md.append(f"  - match `{t.get('matchType')}` / intent "
                          f"`{t.get('intent')}` / confidence {t.get('confidence')}")
        idx_md.append("")
    (out / "INDEX.md").write_text("\n".join(idx_md), encoding="utf-8")

    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
