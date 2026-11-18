"""Waits for the Google session and captures the console on the route page.

Output (in `console_capture/`):
  * `dom.html`      -> DOM of the route page (to tweak selectors)
  * `text.txt`      -> visible text
  * `requests.json` -> the XHR requests the console makes (with headers)
  * `summary.json`  -> readable summary

Usage:
    .venv/bin/python console_capture.py [--wait-minutes 30] [--agent clon]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _entorno import probe_targets
from _redact import write_json_private

from cxp.auth import pick_sapisid
from cxp.browser import ConsoleBrowser
from cxp.config import load_config

OUT = Path(__file__).resolve().parent / "console_capture"

# Pages where there are routes we know exist.
TARGETS = probe_targets()


def wait_for_session(cfg, minutes: int) -> bool:
    """Waits until there is a Google session.

    It does NOT open the browser: the loginHelper keeps the persistent profile open
    (lock) and opening it here would break it. It looks at the `storage_state` the
    helper writes when it detects the session and, as a fallback, at the profile's
    cookie SQLite (copied, not opened).
    """
    import shutil
    import sqlite3

    deadline = time.time() + minutes * 60
    state = cfg.state_file
    while time.time() < deadline:
        if state.exists():
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
                names = {c.get("name") for c in data.get("cookies", [])}
                if pick_sapisid(data.get("cookies", [])):
                    print(f"== SESSION DETECTED in {state}. cookies={sorted(names & {'SID','SAPISID','__Secure-3PSID','__Secure-1PSID','HSID','SSID'})}", flush=True)
                    return True
            except Exception:
                pass
        # Fallback: the profile's cookie SQLite (without opening the browser).
        try:
            src = cfg.real_profile_dir / "Default" / "Cookies"
            tmp = Path("/tmp/_ck_probe.db")
            shutil.copy(src, tmp)
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            rows = list(conn.execute(
                "select name, encrypted_value from cookies where host_key like '%google%'"
            ))
            conn.close()
            if any(n in ("__Secure-3PSID", "SAPISID", "__Secure-1PSID") for n, _ in rows):
                print(f"== SESSION DETECTED in the real profile: {[n for n, _ in rows]}", flush=True)
                return True
        except Exception:
            pass
        time.sleep(10)
    print("== the wait ended without a session", flush=True)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-minutes", type=int, default=30)
    ap.add_argument("--agent", choices=list(TARGETS), default="clon")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--settle", type=int, default=9000, help="wait ms after loading")
    args = ap.parse_args()

    cfg = load_config()
    OUT.mkdir(exist_ok=True)
    if not args.no_wait:
        if not wait_for_session(cfg, args.wait_minutes):
            return 1

    tgt = TARGETS[args.agent]
    url = cfg.console_url(tgt["agent"], tgt["flow"], tgt["page"])
    print(f"== URL: {url}", flush=True)

    # The real Chrome that is already open (with the user session) is REUSED
    # by connecting via CDP: it is not relaunched and the session is not lost.
    from cxp.chrome_cdp import RealChrome

    ch = RealChrome(display=cfg.display, profile_dir=cfg.real_profile_dir, port=cfg.cdp_port)
    br = ConsoleBrowser(cfg)
    br.use_proxy = False
    try:
        page = ch.connect()
        print("== connected via CDP to the real Chrome", flush=True)
    except Exception as exc:
        print(f"== could not connect to the real Chrome: {exc}", flush=True)
        print("== launching a new one with the profile (with a session if it is saved)", flush=True)
        ch.launch(url)
        page = ch.connect()
    br._page = page
    br._ctx = page.context
    br._browser = ch._browser
    br.start_recording()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        print(f"== the navigation to the console failed: {type(exc).__name__}: {str(exc)[:90]}", flush=True)
    time.sleep(args.settle / 1000)
    # Let the console resolve its tabs
    for label in ("Routes", "Transition routes", "Rutas", "Routes"):
        try:
            loc = br._page.get_by_text(label, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                print(f"== tab '{label}' opened", flush=True)
                time.sleep(3)
                break
        except Exception:
            continue

    dom = br._page.content()
    try:
        text = br._page.inner_text("body")
    except Exception:
        text = "<no text>"
    requests = br.stop_recording()

    (OUT / f"dom_{args.agent}.html").write_text(dom, encoding="utf-8")
    (OUT / f"text_{args.agent}.txt").write_text(text, encoding="utf-8")
    write_json_private(OUT / f"requests_{args.agent}.json", requests)

    summary = {
        "url": br._page.url,
        "title": br._page.title(),
        "dom_bytes": len(dom),
        "text": text[:4000],
        "requests": [
            {"method": r["method"], "url": r["url"], "status": r.get("status"),
             "post_len": len(r.get("post_data") or "")}
            for r in requests
        ],
    }
    (OUT / f"summary_{args.agent}.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print("== capture saved in", OUT, flush=True)
    print("== title:", summary["title"], flush=True)
    for p in summary["requests"]:
        print(f"   {p['status']} {p['method']} {p['url'][:130]}", flush=True)
    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
