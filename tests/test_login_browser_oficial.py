"""The LOGIN may only happen in the OFFICIAL Google Chrome.

Google invalidates the sessions created in Chromium or in a "Chrome for Testing"
build: the sign-in looks like it works and then the session dies on its own
(401 / /v3/signin/rejected). So:

1. the browser binaries are classified (official vs experimental);
2. `find_chrome(official_only=True)` returns the official one or None;
3. the browser refuses to open a LOGIN window in a non-official browser and
   explains what to do instead.
"""
from __future__ import annotations

import pytest

from cxp.chrome_cdp import (
    CHROME_CANDIDATES,
    EXPERIMENTAL_CHROME_CANDIDATES,
    INSTALL_OFFICIAL_CHROME,
    OFFICIAL_CHROME_CANDIDATES,
    is_official_chrome,
)


# ------------------------------------------------------------ classification
def test_el_chrome_oficial_se_reconoce():
    assert is_official_chrome("/opt/google/chrome/chrome") is True
    assert is_official_chrome("/usr/bin/google-chrome") is True
    assert is_official_chrome("/usr/bin/google-chrome-stable") is True


def test_chromium_y_chrome_for_testing_no_son_chrome_oficial():
    """A login in these browsers is invalidated by Google."""
    assert is_official_chrome("/usr/bin/chromium") is False
    assert is_official_chrome("/usr/bin/chromium-browser") is False
    assert is_official_chrome("/opt/chrome-cft/chrome-linux64/chrome") is False
    assert is_official_chrome(None) is False


def test_el_chrome_oficial_va_primero_en_la_lista():
    """The official Chrome is preferred over the experimental ones."""
    lista = CHROME_CANDIDATES
    assert lista[: len(OFFICIAL_CHROME_CANDIDATES)] == OFFICIAL_CHROME_CANDIDATES
    for experimental in EXPERIMENTAL_CHROME_CANDIDATES:
        assert experimental not in OFFICIAL_CHROME_CANDIDATES


def test_find_chrome_official_only_ignora_a_los_experimentales(monkeypatch):
    """With only Chromium installed, the official lookup returns None."""
    import cxp.chrome_cdp as cc

    monkeypatch.setattr(cc.Path, "exists", lambda self: False)
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    assert cc.find_chrome() is None
    assert cc.find_chrome(official_only=True) is None


# ------------------------------------------------------------- el login
def test_el_login_no_se_abre_en_un_navegador_no_oficial(monkeypatch):
    """The browser refuses to open a login window outside the official Chrome."""
    import cxp.browser as br
    import cxp.chrome_cdp as cc

    monkeypatch.setattr(cc, "find_chrome",
                        lambda official_only=False: (
                            None if official_only else "/usr/bin/chromium"))

    navegador = br.ConsoleBrowser(br.Config())
    with pytest.raises(RuntimeError) as exc:
        navegador._assert_login_browser_is_official()
    mensaje = str(exc.value)
    assert "invalidat" in mensaje
    assert "Chrome for Testing" in mensaje
    assert INSTALL_OFFICIAL_CHROME.split(":")[0] in mensaje


def test_con_chrome_oficial_el_login_si_puede_abrirse(monkeypatch):
    import cxp.browser as br
    import cxp.chrome_cdp as cc

    monkeypatch.setattr(cc, "find_chrome",
                        lambda official_only=False: "/opt/google/chrome/chrome")

    navegador = br.ConsoleBrowser(br.Config())
    navegador._assert_login_browser_is_official()  # does not raise
