"""Control of Chrome REAL via MCP (low-level actions on the console).

MCP does not use a public API for this: the "Conversational Agents" console
calls an internal backend (`dialogflow.clients6.google.com`) with
first-party authentication (Google cookies) and with a proto that DOES
expose `targetPlaybook` in `TransitionRoute`. That is why the real
browser must be driven.

All actions return a dict with what happened (they do not throw
exceptions to the caller) so the agent can decide the next step."""
from __future__ import annotations

import time
from typing import Any

from .browser import ConsoleBrowser


class BrowserControlError(RuntimeError):
    pass


def _page(br: ConsoleBrowser):
    if br._page is None:
        br._launch(headless=False)
    return br._page


# --------------------------------------------------------------------- actions
def ctl_status(br: ConsoleBrowser) -> dict:
    page = _page(br)
    out: dict[str, Any] = {}
    try:
        out["url"] = page.url
        out["title"] = page.title()
        out["ua"] = page.evaluate("navigator.userAgent")
        out["secure_context"] = page.evaluate("window.isSecureContext")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    cookies = br.cookies()
    names = sorted({c.get("name") for c in cookies})
    out["cookies_google"] = [n for n in names if "PSID" in n or n in ("SID", "SAPISID", "HSID", "SSID")]
    out["total_cookies"] = len(cookies)
    out["sesion_google"] = bool(set(out["cookies_google"]) & {"__Secure-3PSID", "SAPISID", "SID"})
    try:
        out["pestanas"] = [p.url for p in page.context.pages]
    except Exception:
        pass
    return out


def ctl_open(br: ConsoleBrowser, url: str, wait_ms: int = 3000) -> dict:
    page = _page(br)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception as exc:
        return {"ok": False, "error": f"goto: {type(exc).__name__}: {exc}", "url": page.url}
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    return {"ok": True, "url": page.url, "title": page.title()}


def ctl_text(br: ConsoleBrowser, selector: str = "body", limit: int = 6000) -> dict:
    page = _page(br)
    try:
        if selector == "body":
            txt = page.inner_text("body")
        else:
            loc = page.locator(selector)
            txt = "\n".join(t for t in loc.all_inner_texts())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "selector": selector, "text": txt[:limit], "length": len(txt)}


def ctl_click(br: ConsoleBrowser, selector: str = "", text: str = "", nth: int = 0,
              timeout: float = 15.0) -> dict:
    """Click an element by selector CSS or by visible text."""
    page = _page(br)
    loc = None
    if text:
        loc = page.get_by_text(text, exact=False)
    elif selector:
        loc = page.locator(selector)
    else:
        return {"ok": False, "error": "indicate selector= or text="}
    try:
        n = loc.count()
    except Exception as exc:
        return {"ok": False, "error": f"count: {type(exc).__name__}: {exc}"}
    if n == 0:
        return {"ok": False, "error": "the element was not found", "matches": 0}
    idx = min(max(nth, 0), n - 1)
    try:
        target = loc.nth(idx)
        target.scroll_into_view_if_needed(timeout=timeout * 1000)
        target.click(timeout=timeout * 1000)
    except Exception as exc:
        return {"ok": False, "error": f"click: {type(exc).__name__}: {exc}", "matches": n}
    page.wait_for_timeout(800)
    return {"ok": True, "matches": n, "clicked_index": idx, "url": page.url}


def ctl_fill(br: ConsoleBrowser, selector: str, value: str, timeout: float = 15.0) -> dict:
    page = _page(br)
    try:
        loc = page.locator(selector).first
        loc.fill(value, timeout=timeout * 1000)
    except Exception as exc:
        return {"ok": False, "error": f"fill: {type(exc).__name__}: {exc}"}
    return {"ok": True, "selector": selector}


def ctl_press(br: ConsoleBrowser, key: str = "Enter") -> dict:
    page = _page(br)
    try:
        page.keyboard.press(key)
    except Exception as exc:
        return {"ok": False, "error": f"press: {type(exc).__name__}: {exc}"}
    page.wait_for_timeout(500)
    return {"ok": True, "key": key}


def ctl_screenshot(br: ConsoleBrowser, path: str = "/tmp/cx-playbook-console.png",
                   full_page: bool = True) -> dict:
    page = _page(br)
    try:
        page.screenshot(path=path, full_page=full_page)
    except Exception as exc:
        return {"ok": False, "error": f"screenshot: {type(exc).__name__}: {exc}"}
    return {"ok": True, "path": path, "url": page.url}


def ctl_eval(br: ConsoleBrowser, js: str) -> dict:
    page = _page(br)
    try:
        val = page.evaluate(js)
    except Exception as exc:
        return {"ok": False, "error": f"eval: {type(exc).__name__}: {exc}"}
    return {"ok": True, "resultado": val}


def ctl_requests(br: ConsoleBrowser) -> dict:
    """Returns the captured XHR requests (start_recording must be called first)."""
    reqs = br.stop_recording()
    return {
        "ok": True,
        "n": len(reqs),
        "requests": [
            {
                "method": r.get("method"),
                "url": r.get("url"),
                "status": r.get("status"),
                "post_len": len(r.get("post_data") or ""),
                "post_data": (r.get("post_data") or "")[:4000],
                "headers": {k: v for k, v in (r.get("headers") or {}).items()
                            if k.lower() in ("content-type", "x-goog-user-project", "authorization",
                                             "x-goog-request-params", "referer", "origin")},
            }
            for r in reqs
        ],
    }


def ctl_wait(br: ConsoleBrowser, ms: int = 2000) -> dict:
    page = _page(br)
    page.wait_for_timeout(int(ms))
    return {"ok": True, "esperado_ms": ms, "url": page.url}


# --------------------------------------------------------------------- dispatch
ACTIONS = {
    "status": ctl_status,
    "open": ctl_open,
    "text": ctl_text,
    "click": ctl_click,
    "fill": ctl_fill,
    "press": ctl_press,
    "screenshot": ctl_screenshot,
    "eval": ctl_eval,
    "requests": ctl_requests,
    "wait": ctl_wait,
}


def dispatch(br: ConsoleBrowser, action: str, **kw) -> dict:
    fn = ACTIONS.get(action)
    if fn is None:
        return {
            "ok": False,
            "error": f"unknown action: {action}",
            "actions": sorted(ACTIONS),
        }
    try:
        return fn(br, **kw)
    except TypeError as exc:
        return {"ok": False, "error": f"invalid arguments for '{action}': {exc}",
                "actions": sorted(ACTIONS)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------- recipes UI
def list_selectors(br: ConsoleBrowser, limit: int = 120) -> dict:
    """List the interactive elements of the page (to refine selectors)."""
    page = _page(br)
    js = """
    () => {
      const out = [];
      const sel = 'button,a[role=button],[role=tab],[role=menuitem],[role=option],'
                + 'mat-option,[role=listbox] [role=option],input,textarea,'
                + '[ng-reflect-message],[class*=route],[class*=transition]';
      document.querySelectorAll(sel).forEach((el) => {
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        if (!t || t.length > 120) return;
        out.push({tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 80),
                  role: el.getAttribute('role') || '', text: t.slice(0, 90),
                  visible: !!(el.offsetParent || el.getClientRects().length)});
      });
      return out.slice(0, __LIMIT__);
    }
    """.replace("__LIMIT__", str(limit))
    try:
        items = page.evaluate(js)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "n": len(items), "elementos": items}


def wait_for_text(br: ConsoleBrowser, text: str, timeout_s: float = 30.0) -> dict:
    """Wait for text to appear on the page."""
    page = _page(br)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count() > 0:
                try:
                    vis = any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))
                except Exception:
                    vis = True
                if vis:
                    return {"ok": True, "found": text, "url": page.url}
        except Exception:
            pass
        page.wait_for_timeout(700)
    return {"ok": False, "error": f"timeout waiting for '{text}'", "url": page.url}
