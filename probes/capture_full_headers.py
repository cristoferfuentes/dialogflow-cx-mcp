#!/usr/bin/env python3
"""Captures the FULL HEADERS of a request the console makes to the
internal backend (clients6) and tries to replay it from the page itself.

Output: /tmp/full_headers.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import targets_de_sonda

from cxp.config import load_config

# The target (agent, flow, page) comes from the environment:
#   CXP_PROBE_TARGETS="por_defecto=agent,flow,page"
TARGET = next(iter(targets_de_sonda().values()), {"agent": "", "flow": "", "page": ""})
URL = (
    "https://conversational-agents.cloud.google.com/projects/{p}/locations/global"
    f"/agents/{TARGET['agent']}/(cx/flows/{TARGET['flow']}/flow_creation//right-panel:simulator)"
    f"?pageId={TARGET['page']}"
)


def main() -> int:
    cfg = load_config()
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    page = ch.connect()
    ctx = page.context

    captured: list[dict] = []

    def on_req(req):
        if "clients6.google.com" not in req.url:
            return
        try:
            hdrs = req.all_headers()
        except Exception:
            hdrs = req.headers
        captured.append({"url": req.url, "method": req.method, "headers": hdrs,
                         "post_data": (req.post_data or "")[:3000]})

    ctx.on("request", on_req)
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        print(f"== goto failed: {exc}")
    time.sleep(9)

    flows = [c for c in captured if "/flows?" in c["url"]]
    print(f"== captured requests: {len(captured)} (flows: {len(flows)})")
    out = {}
    if flows:
        f = flows[-1]
        out["sample_flow_request"] = f
        print("\n== FULL HEADERS of the flows request:")
        for k, v in sorted(f["headers"].items()):
            print(f"   {k}: {v[:160]}")
        print(f"\n== url: {f['url']}")

    # Replay the call from the page: the browser adds its cookies.
    if flows:
        hdrs = {k: v for k, v in flows[-1]["headers"].items()
                if k.lower() not in ("cookie", "host", "content-length", "origin",
                                     "referer", "sec-fetch-site", "sec-fetch-mode",
                                     "sec-fetch-dest", "accept-encoding", "connection")}
        try:
            res = page.evaluate(
                """async ({url, headers}) => {
                    const r = await fetch(url, {method: 'GET', credentials: 'include', headers});
                    return {status: r.status, body: (await r.text()).slice(0, 400)};
                }""",
                {"url": flows[-1]["url"], "headers": hdrs},
            )
            print("\n== replayed FROM THE PAGE:", res["status"])
            print("   ", " ".join(res["body"].split())[:300])
            out["replay_in_page"] = res
        except Exception as exc:
            print(f"\n== replay failed: {type(exc).__name__}: {exc}")
            out["replay_in_page"] = {"error": str(exc)}

    Path("/tmp/full_headers.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n== written /tmp/full_headers.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
