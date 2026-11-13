#!/usr/bin/env python3
"""Experimento: hacer la llamada al backend interno desde `static/proxy.html`
(mismo origen que el backend, igual que la consola) y capturar las cabeceras
REALES de cable (`requestWillBeSentExtraInfo`).

Sirve para comparar con lo que manda Python y ver que cabecera falta.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cxp.config import load_config

from _entorno import agente, targets_de_sonda
from cxp.console_api import CONSOLE_HOST, sapisid_authorization

ORIG = agente("por_defecto")
_TARGET = next(iter(targets_de_sonda().values()), {"flow": "", "page": ""})
OF = _TARGET["flow"]
OP = _TARGET["page"]


def main() -> int:
    cfg = load_config()
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    page = ch.connect()

    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    wire: list[dict] = []

    def on_extra(ev):
        wire.append({"requestId": ev.get("requestId"), "headers": ev.get("headers") or {},
                     "associatedCookies": [
                         {"name": c.get("cookie", {}).get("name"),
                          "domain": c.get("cookie", {}).get("domain")}
                         for c in (ev.get("associatedCookies") or [])]})

    cdp.on("Network.requestWillBeSentExtraInfo", on_extra)

    page.goto(f"https://{CONSOLE_HOST}/static/proxy.html?usegapi=1",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(3)
    print("== en proxy.html:", page.url)

    # Cookies vivas para el hash
    live = {c["name"]: c["value"] for c in page.context.cookies() if c.get("value")}
    sapisid = live.get("SAPISID") or live.get("__Secure-3PSID")
    ts = int(time.time())
    auth = sapisid_authorization(cfg.console_origin, sapisid, ts)

    url = (f"https://{CONSOLE_HOST}/v3alpha1/projects/{cfg.project}/locations/global/agents/{ORIG}"
           f"/flows/{OF}/pages/{OP}?key={cfg.api_key}&languageCode=es")
    headers = {
        "Authorization": auth,
        "X-Origin": cfg.console_origin,
        "X-Referer": cfg.console_origin,
        "X-Goog-AuthUser": "0",
        "X-Goog-Encode-Response-If-Executable": "base64",
        "X-JavaScript-User-Agent": "google-api-javascript-client/1.1.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    res = page.evaluate(
        """async ({url, headers}) => {
            const r = await fetch(url, {method:'GET', credentials:'include', headers});
            return {status: r.status, body: (await r.text()).slice(0, 500)};
        }""",
        {"url": url, "headers": headers},
    )
    print("== fetch desde proxy.html:", res["status"])
    print("   ", res["body"].replace("\n", " ")[:300])

    time.sleep(2)
    print(f"\n== cabeceras de cable capturadas: {len(wire)}")
    for w in wire[-3:]:
        print("\n--- requestId", w["requestId"])
        for k, v in sorted(w["headers"].items()):
            print(f"    {k}: {v[:150]}")
        print("    cookies:", [c["name"] for c in w["associatedCookies"]][:30])

    Path("/tmp/wire.json").write_text(json.dumps(wire, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
