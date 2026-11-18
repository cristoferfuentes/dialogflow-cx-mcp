"""The REAL Chrome (not Playwright's "Chrome for Testing", not headless) driven by CDP.

Why it is needed:
* Playwright launches `HeadlessChrome/156` -> Google REJECTS the sign-in of accounts
  ("You can't access this: it may be that this browser or the app is not secure"
  -> /v3/signin/rejected).
* The MITM proxy terminates TLS with Python: the client's JA3/JA4 is
  OpenSSL's, not Chrome's -> Google can also flag it as odd.
* A desktop Chrome, with a window and its own TLS, is what most resembles
  a real browser.

Control is via **CDP** (`--remote-debugging-port`), which is what lets
the MCP drive it just as Playwright drives it, but on the real browser."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

# Chrome binaries, in order of preference.
#
# IMPORTANT (measured): a Google session created in **Chromium** or in a
# **"Chrome for Testing"** build is invalidated by Google: the login "works" and
# then stops working (401 / /v3/signin/rejected). So those browsers can be used
# to READ an existing session, but **never to sign in**.
OFFICIAL_CHROME_CANDIDATES = [
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome-beta",
]

# These work as browsers, but a login made in them is invalidated by Google.
EXPERIMENTAL_CHROME_CANDIDATES = [
    "/opt/chrome-cft/chrome-linux64/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

CHROME_CANDIDATES = OFFICIAL_CHROME_CANDIDATES + EXPERIMENTAL_CHROME_CANDIDATES

# How to install the official Chrome on a Debian/Ubuntu machine.
INSTALL_OFFICIAL_CHROME = (
    "install the official Google Chrome (the only browser where a Google login "
    "survives): "
    "wget -q -O - https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb "
    "| sudo apt-get install -y -"
)


def is_official_chrome(path: str | None) -> bool:
    """Is this binary the OFFICIAL Google Chrome (and not Chromium / CFT)?"""
    if not path:
        return False
    p = str(path)
    if any(p.startswith(c) for c in OFFICIAL_CHROME_CANDIDATES):
        return True
    base = Path(p).name.lower()
    if "chromium" in base:
        return False
    if "chrome-cft" in p or "chrome-for-testing" in p or "chrome-cft" in base:
        return False
    # `google-chrome`, `google-chrome-stable`, ... are official.
    return base.startswith("google-chrome")


def find_chrome(official_only: bool = False) -> str | None:
    """The Chrome binary of this machine.

    `official_only=True` returns the official Google Chrome (or None): it is
    what a LOGIN needs."""
    candidates = OFFICIAL_CHROME_CANDIDATES if official_only else CHROME_CANDIDATES
    for p in candidates:
        if Path(p).exists():
            return p
    found = shutil.which("google-chrome")
    if official_only:
        return found if is_official_chrome(found) else None
    return found or shutil.which("chromium")


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class RealChrome:
    """Desktop Chrome with port CDP, on a virtual display."""

    def __init__(
        self,
        display: str = ":99",
        profile_dir: str | Path = "/tmp/cx-playbook-chrome-profile",
        port: int = 9222,
        window_size: str = "1280,740",
        extra_args: list[str] | None = None,
        headless: bool = False,
    ):
        self.display = display
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.window_size = window_size
        self.extra_args = extra_args or []
        self.headless = headless
        self.proc: subprocess.Popen | None = None
        self.binary: str | None = None
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page = None

    # ------------------------------------------------------------- cycle
    def launch(self, start_url: str = "about:blank") -> str:
        binary = find_chrome()
        if not binary:
            raise RuntimeError(
                "no real Chrome installed (looked in "
                + ", ".join(CHROME_CANDIDATES) + ")"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "DISPLAY": self.display}
        base = [
            binary,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--lang=es-419",
            "--accept-lang=es-419,es;q=0.9",
            f"--window-size={self.window_size}",
            "--window-position=0,0",
            # Without UA: the UA must be that of a desktop Chrome.
            "--disable-features=Translate,TranslateUI,OptimizationHints",
            # --test-type: Chrome does not show the "unsupported
            # command-line flag" warning (the screen stays clean for login).
            "--test-type",
            *self.extra_args,
            start_url,
        ]
        if self.headless:
            base.insert(1, "--headless=new")
        # First it tries WITH sandbox (Chrome complains about the --no-sandbox flag).
        for extra in ([], ["--no-sandbox", "--disable-dev-shm-usage"]):
            args = base[:1] + extra + base[1:]
            self.proc = subprocess.Popen(
                args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.time() + 40
            while time.time() < deadline:
                if port_open(self.port):
                    self.binary = binary
                    return binary
                if self.proc.poll() is not None:
                    break
                time.sleep(0.4)
            # Did not open the port: retrying with --no-sandbox.
            try:
                self.proc.terminate()
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError(
            "Chrome did not open the CDP port. If this machine has no desktop, "
            "start a virtual display (Xvfb :99) and set CXP_DISPLAY=:99, or take "
            "the cookie manually from your own browser"
        )

    def connect(self) -> Any:
        """Connects via CDP and returns the context with the first tab."""
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.port}", timeout=60_000
        )
        ctxs = self._browser.contexts
        if not ctxs:
            raise RuntimeError("Chrome has no contexts")
        self._ctx = ctxs[0]
        pages = self._ctx.pages
        self.page = pages[0] if pages else self._ctx.new_page()
        self.page.set_default_timeout(45_000)
        return self.page

    def ensure(self, start_url: str = "about:blank") -> Any:
        if self.page is None:
            self.launch(start_url)
            self.connect()
        return self.page

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = None
        self.page = None
        # The process stays (it is the user's browser); it is closed separately.
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------- info
    def info(self) -> dict:
        """UA, version and status of the CDP (useful for diagnostics)."""
        out: dict = {"binary": find_chrome(), "port": self.port, "pid": self.proc.pid if self.proc else None}
        try:
            req = subprocess.run(
                ["curl", "-s", f"http://127.0.0.1:{self.port}/json/version"],
                capture_output=True, text=True, timeout=10,
            )
            out["cdp"] = json.loads(req.stdout)
        except Exception as exc:
            out["cdp_error"] = str(exc)
        if self.page is not None:
            try:
                out["ua"] = self.page.evaluate("navigator.userAgent")
                out["secure"] = self.page.evaluate("window.isSecureContext")
            except Exception as exc:
                out["page_error"] = str(exc)
        return out
