"""Browser: persistent profile + first launch with a window (login) + headless.

Automatic launch mechanism:

* The FIRST time the MCP is invoked, if there is no valid Google session, a
  Chromium is opened **with a window** (headed) so the user can log in to their
  account. The profile (cookies) is persisted in `profile_dir`.
* From then on, all invocations run **headless** with that profile: no
  window appears and the latency is that of a HTTP call.

It also captures the browser traffic, so it can learn (or replicate) the exact
request the console makes."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from .auth import first_party_headers, pick_google_cookies, pick_sapisid
from .config import Config
from .mitm import MitmProxy

# Hosts that the local proxy must know how to answer.
MITM_HOSTS = [
    "conversational-agents.cloud.google.com",
    "accounts.google.com",
    "google.com",
    "*.google.com",
    "*.googleapis.com",
    "*.gstatic.com",
    "*.clients6.google.com",
]


# Hosts into which `Authorization: Bearer <token ADC>` can be injected
# when the browser has NO Google session (first part / UDS).
BEARER_HOSTS = {
    "dialogflow.clients6.google.com",
    "conversational-agents.cloud.google.com",
    "global-dialogflow.googleapis.com",
}


@dataclass
class SessionState:
    logged_in: bool
    user_email: str | None = None
    reason: str = ""
    cookies: list[dict] | None = None


class ConsoleBrowser:
    """Persistent Chromium profile for the Conversational Agents console."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._ctx = None
        self._page = None
        self._proxy: MitmProxy | None = None
        self.captured: list[dict] = []
        self._record = False
        self._token: str | None = None
        self._token_ts: float = 0.0
        # Extra Chromium flags (e.g. "--disable-quic"). They are added to the
        # startup arguments; useful for network diagnostics.
        self.extra_args: list[str] = []
        # If False, the browser does NOT use the local MITM proxy (native TLS).
        self.use_proxy = True
        # Inject `Authorization: Bearer <token ADC>` into browser requests. NOTE: by default DISABLED: when adding a
        # `Authorization` header to a XHR cross-origin, the browser triggers a
        # CORS preflight and the proxy also signs the OPTIONS -> the browser
        # rejects the response and the console stays blank (measured).
        # To talk to the backend without a Google session, use the calls
        # from Python (cxp/cxapi.py), which have no CORS.
        self.inject_bearer = False
        # Use the `storage_state` (cookies) instead of the persistent profile.
        # Avoid the profile lock if the login is still open in another browser.
        self.use_state_file = False
        # Use desktop CHROME REAL controlled by CDP instead of
        # Playwright's Chromium. Google rejects login in headless browsers
        # and the MITM proxy changes the TLS's JA3: with real Chrome
        # (native TLS, with a window) both login and console work.
        self.use_real_chrome = True
        self._real = None
        self._browser = None
        # User-Agent. Google REJECTS account login in headless browsers
        # ("You can't access this: it may be that this browser or the app
        # is not secure" -> /v3/signin/rejected) because its UA says
        # "HeadlessChrome". A desktop Chrome UA is presented.
        # (The session is the user's own, on their own machine.)
        self.user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/156.0.0.0 Safari/537.36"
        )

    # ------------------------------------------------------------------ cycle
    def _bearer_token(self) -> str | None:
        """OAuth token for ADC (gcloud), cached ~50 min (expires in 1 h)."""
        if self._token and (time.time() - self._token_ts) < 3000:
            return self._token
        from .auth import gcloud_access_token

        tok = gcloud_access_token()
        if tok:
            self._token, self._token_ts = tok, time.time()
        return tok

    def _window_size(self) -> str:
        """Window size = that of the virtual screen (Xvfb :99 = 1280x800).

        If the window is larger than the screen, the window manager leaves it
        out of view and the user sees nothing through noVNC."""
        try:
            import subprocess

            out = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "DISPLAY": self.cfg.display},
            )
            w, h = (int(x) for x in out.stdout.split()[:2])
            return f"{w},{max(600, h - 60)}"
        except Exception:
            return "1280,760"

    # ------------------------------------------------------------------ cycle
    def _launch_real_chrome(self, headless: bool):
        """Use Chrome REAL (via CDP) instead of Playwright's Chromium.

        Google rejects login from headless browsers
        (`/v3/signin/rejected`) and the MITM proxy changes the JA3 of the TLS. With a
        desktop Chrome (Google's, installed at `/opt/google/chrome`)
        and native TLS, the session and the console actually work."""
        from .chrome_cdp import RealChrome

        if self._real is None:
            self._real = RealChrome(
                display=self.cfg.display,
                profile_dir=self.cfg.real_profile_dir,
                port=self.cfg.cdp_port,
                headless=headless,
            )
        try:
            page = self._real.connect()
        except Exception:
            # There was no Chrome listening: it is launched (with the package profile).
            self._real.launch("about:blank")
            page = self._real.connect()
        self._ctx = page.context
        self._browser = self._real._browser
        self._ctx.set_default_timeout(45_000)
        self._ctx.on("request", self._on_request)
        self._ctx.on("response", self._on_response)
        self._page = page
        return page

    def _launch(self, headless: bool):
        from playwright.sync_api import sync_playwright

        # The virtual display must exist BEFORE starting the Playwright driver: the browser is a child of the driver and inherits its environment.
        if self.cfg.display:
            os.environ["DISPLAY"] = self.cfg.display

        # --- Chrome REAL (by CDP) ------------------------------------------
        if self.use_real_chrome:
            return self._launch_real_chrome(headless)

        if self._pw is None:
            self._pw = sync_playwright().start()
        # The browser cannot make outbound TLS on this machine: the local MITM proxy is set up (see cxp/mitm.py) so that the TLS with Google is done by
        # Python, which does work.
        if self.use_proxy and self._proxy is None:
            self._proxy = MitmProxy(
                self.cfg.profile_dir.parent / ".proxy-cert",
                MITM_HOSTS,
                quota_project=self.cfg.quota_project,
                bearer_token=self._bearer_token() if self.inject_bearer else None,
                bearer_hosts=BEARER_HOSTS,
            ).start()
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={self._window_size()}",
            "--window-position=0,0",
            "--ignore-certificate-errors",
            *self.extra_args,
        ]
        if self.use_proxy and self._proxy is not None:
            args.append(f"--proxy-server={self._proxy.url}")
        kwargs = dict(
            user_data_dir=str(self.cfg.profile_dir),
            headless=headless,
            args=args,
            locale="es-419",
            timezone_id="America/Santiago",
            user_agent=self.user_agent,
            env=dict(os.environ),
        )
        if headless:
            # In headless there is no window: the viewport defines the size.
            kwargs["viewport"] = {"width": 1280, "height": 900}
        else:
            # With a real window, the fixed viewport fights with --window-size and leaves
            # the window larger than the virtual screen (not visible through noVNC).
            kwargs["no_viewport"] = True
        state = self.cfg.state_file
        use_state = bool(state.exists() and self.use_state_file)
        try:
            # `channel="chromium"` uses the full Chromium build in new
            # headless mode: much more stable with Google sessions than
            # `chromium_headless_shell`.
            if use_state:
                # TEMPORARY context with the `storage_state` cookies: does not
                # touch the persistent profile, so it does not clash with the lock if
                # the login is still open in another browser.
                browser = self._pw.chromium.launch(channel="chromium", headless=headless, args=args, env=dict(os.environ))
                self._ctx = browser.new_context(
                    storage_state=str(state),
                    locale="es-419",
                    timezone_id="America/Santiago",
                    user_agent=self.user_agent,
                    **({"viewport": {"width": 1280, "height": 900}} if headless else {"no_viewport": True}),
                )
                self._browser = browser
            else:
                self._ctx = self._pw.chromium.launch_persistent_context(channel="chromium", **kwargs)
        except Exception:
            if use_state:
                browser = self._pw.chromium.launch(headless=headless, args=args, env=dict(os.environ))
                self._ctx = browser.new_context(
                    storage_state=str(state),
                    locale="es-419",
                    timezone_id="America/Santiago",
                    user_agent=self.user_agent,
                    **({"viewport": {"width": 1280, "height": 900}} if headless else {"no_viewport": True}),
                )
                self._browser = browser
            else:
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self._ctx.set_default_timeout(30_000)
        self._ctx.on("request", self._on_request)
        self._ctx.on("response", self._on_response)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self._page

    def close(self):
        """Closes what MCP has opened.

        Important: in Chrome REAL mode the browser belongs to USER (it is their
        Google session). It is NOT closed: only the CDP connection is released, so
        the login stays alive and MCP can reconnect."""
        try:
            if self._ctx and not self.use_real_chrome:
                self._ctx.close()
        except Exception:
            pass
        finally:
            self._ctx = None
            self._page = None
            if self._browser is not None and not self.use_real_chrome:
                try:
                    self._browser.close()
                except Exception:
                    pass
            self._browser = None
            if self._proxy:
                self._proxy.stop()
                self._proxy = None
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            if self.use_real_chrome:
                # The real Chrome stays open: only the driver is released.
                self._real = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- capture
    def _on_request(self, request):
        if not self._record:
            return
        url = request.url
        if "clients6.google.com" not in url and "dialogflow" not in url:
            return
        try:
            headers = request.headers
        except Exception:
            headers = {}
        self.captured.append(
            {
                "method": request.method,
                "url": url,
                "headers": {k: v for k, v in headers.items() if k.lower() not in ("cookie",)},
                "post_data": (request.post_data or "")[:20000],
                "ts": time.time(),
            }
        )

    def _on_response(self, response):
        if not self._record:
            return
        try:
            if "clients6.google.com" not in response.url:
                return
            body = response.text()[:4000]
        except Exception:
            return
        for entry in reversed(self.captured):
            if entry["url"] == response.url and entry["method"] == response.request.method:
                entry["status"] = response.status
                entry["response_preview"] = body
                break

    def start_recording(self):
        self.captured = []
        self._record = True

    def stop_recording(self) -> list[dict]:
        self._record = False
        return self.captured

    # ---------------------------------------------------------------- session
    def _looks_logged_in(self) -> tuple[bool, str, str | None]:
        """The real session signal is the Google cookies (not the URL)."""
        try:
            url = self._page.url
        except Exception:
            return False, "no page", None
        cookies = self.cookies()
        names = {c.get("name") for c in cookies}
        sapisid = pick_sapisid(cookies)
        if not sapisid and "SID" not in names:
            return False, "without Google session cookies (SID/__Secure-3PSID)", None
        if "accounts.google.com" in url:
            return False, "redirigido a accounts.google.com", None
        try:
            title = (self._page.title() or "").lower()
        except Exception:
            title = ""
        if "sign in" in title or "iniciar sesión" in title:
            return False, "login title", None
        email = self._page_email()
        return True, f"url={url} | cookies={sorted(names & {'SID', 'SAPISID', '__Secure-3PSID', 'HSID', 'SSID'})}", email

    def _page_email(self) -> str | None:
        for sel in ("#gAIzaun", "[aria-label*='Cuenta de Google']", "[aria-label*='Google Account']"):
            try:
                el = self._page.query_selector(sel)
                if el:
                    val = (el.get_attribute("aria-label") or "").strip()
                    if "@" in val:
                        return val.split("(")[0].strip()
            except Exception:
                continue
        try:
            for cookie in self._ctx.cookies():
                if cookie.get("name") == "ACCOUNT_CHOOSER" and cookie.get("value"):
                    return None
        except Exception:
            pass
        return None

    def open_console(self, url: str, wait_ms: int = 3500):
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._page.wait_for_timeout(wait_ms)

    def ensure_session(self, console_url: str, headed_timeout: int = 600) -> SessionState:
        """Returns a valid session. If none exists, opens a window for login."""
        self._launch(headless=True)
        self.open_console(console_url)
        ok, reason, email = self._looks_logged_in()
        if ok:
            return SessionState(True, email, reason)

        # No session -> the window opens so the user can sign in to Google.
        self.close()
        self._launch(headless=False)
        self.open_console(console_url)
        deadline = time.time() + headed_timeout
        while time.time() < deadline:
            ok, reason, email = self._looks_logged_in()
            if ok:
                # Store cookies explicitly.
                try:
                    self._ctx.storage_state(path=str(self.cfg.state_file))
                except Exception:
                    pass
                return SessionState(True, email, f"login completado: {reason}")
            time.sleep(2)
        self.close()
        return SessionState(False, None, "timeout waiting for the login to start")

    # ---------------------------------------------------------------- cookies
    def cookies(self) -> list[dict]:
        if not self._ctx:
            return []
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def auth_headers(self) -> dict[str, str]:
        """First-party headers (UDS) equivalent to those of the console."""
        cookies = self.cookies()
        sapisid = pick_sapisid(cookies)
        if not sapisid:
            raise RuntimeError("without the SAPISID cookie: the Google session is not active")
        headers = first_party_headers(self.cfg.console_origin, sapisid)
        extra = pick_google_cookies(cookies)
        if extra:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in extra.items())
        return headers

    def fetch_in_page(self, method: str, url: str, payload: Any = None) -> tuple[int, str]:
        """Makes the FROM request to the page: the browser sends its cookies and CORS."""
        body = None
        if payload is not None:
            body = json.dumps(payload)
        result = self._page.evaluate(
            """async ({method, url, body}) => {
                const opts = {method, credentials: 'include', headers: {}};
                if (body !== null) {
                    opts.body = body;
                    opts.headers['Content-Type'] = 'application/json';
                }
                const r = await fetch(url, opts);
                return {status: r.status, text: await r.text()};
            }""",
            {"method": method, "url": url, "body": body},
        )
        return result["status"], result["text"]
