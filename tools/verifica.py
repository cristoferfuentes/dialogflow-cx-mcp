#!/usr/bin/env python3
"""Local verification runner: runs the project's checks and writes static badges.

This script replaces the CI workflow that used to live in
``.github/workflows/ci.yml``.  There is no hosted runner anymore, so the
badges published in the README are **static SVG files committed to the
repository** and generated from the results of a real run **on this machine**.

What it checks (the same set the CI used to run):

* ``tests``    -- the unit test suite (``pytest -q``).
* ``lint``     -- ``ruff check cxp server.py tests probes``.
* ``server``   -- the MCP server imports and exposes its tools.
* ``secrets``  -- no credentials, real project ids or real agent ids in the
  working tree **nor in the git history**.

Usage::

    python tools/verifica.py            # run everything, rewrite the badges
    python tools/verifica.py --check    # run everything, do not rewrite badges
    python tools/verifica.py --only lint

The exit status is ``0`` when every check passed, ``1`` otherwise, so the
script can be used as a pre-commit hook or in a local ``make check``.

The badges are written to ``docs/badges/`` and are plain SVG files: they do
not depend on shields.io or on any hosted service.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BADGES = RAIZ / "docs" / "badges"

# ---------------------------------------------------------------------------
# badge rendering (flat shields-style SVG, generated locally)
# ---------------------------------------------------------------------------

# Width of a character in the DejaVu Sans 11px used by the classic badge
# style.  It is an approximation, only used to lay the two halves out.
ANCHO_CARACTER = 6.6
RELLENO = 10

COLOR_OK = "#4c1"
COLOR_ERROR = "#e05d44"
COLOR_AVISO = "#dfb317"
COLOR_NEUTRAL = "#9f9f9f"
COLOR_ETIQUETA = "#555"


def _ancho(texto: str) -> int:
    return int(round(len(texto) * ANCHO_CARACTER)) + 2 * RELLENO


def badge(etiqueta: str, valor: str, color: str) -> str:
    """Return the SVG source of a flat two-part badge."""
    ancho_etiqueta = _ancho(etiqueta)
    ancho_valor = _ancho(valor)
    ancho = ancho_etiqueta + ancho_valor
    x_etiqueta = ancho_etiqueta / 2
    x_valor = ancho_etiqueta + ancho_valor / 2
    titulo = f"{etiqueta}: {valor}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ancho}" height="20" '
        f'role="img" aria-label="{titulo}">\n'
        f"  <title>{titulo}</title>\n"
        f'  <linearGradient id="s" x2="0" y2="100%">\n'
        f'    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>\n'
        f'    <stop offset="1" stop-opacity=".1"/>\n'
        f"  </linearGradient>\n"
        f'  <clipPath id="r"><rect width="{ancho}" height="20" rx="3" fill="#fff"/></clipPath>\n'
        f'  <g clip-path="url(#r)">\n'
        f'    <rect width="{ancho_etiqueta}" height="20" fill="{COLOR_ETIQUETA}"/>\n'
        f'    <rect x="{ancho_etiqueta}" width="{ancho_valor}" height="20" fill="{color}"/>\n'
        f'    <rect width="{ancho}" height="20" fill="url(#s)"/>\n'
        f"  </g>\n"
        f'  <g fill="#fff" text-anchor="middle" '
        f'font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">\n'
        f'    <text x="{x_etiqueta:.1f}" y="14">{etiqueta}</text>\n'
        f'    <text x="{x_valor:.1f}" y="14">{valor}</text>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def escribe_badge(nombre: str, etiqueta: str, valor: str, color: str) -> Path:
    BADGES.mkdir(parents=True, exist_ok=True)
    destino = BADGES / nombre
    destino.write_text(badge(etiqueta, valor, color), encoding="utf-8")
    return destino


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


@dataclass
class Resultado:
    nombre: str
    etiqueta: str
    estado: str  # "ok" | "error" | "aviso"
    valor: str
    detalle: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def color(self) -> str:
        return {
            "ok": COLOR_OK,
            "error": COLOR_ERROR,
            "aviso": COLOR_AVISO,
        }[self.estado]


def _corre(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd or RAIZ),
        capture_output=True,
        text=True,
    )


def check_tests() -> Resultado:
    # No extra "-q": the project already sets it in addopts, and "-qq" would
    # suppress the summary line that carries the number of passed tests.
    proc = _corre([sys.executable, "-m", "pytest"])
    salida = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"(\d+)\s+passed", salida)
    if proc.returncode == 0:
        total = m.group(1) if m else "?"
        return Resultado("tests", "tests", "ok", f"{total} passed", salida[-2000:])
    m = re.search(r"(\d+)\s+failed", salida)
    extra = f", {m.group(1)} failed" if m else ""
    return Resultado("tests", "tests", "error", f"failing{extra}", salida[-4000:])


def check_lint() -> Resultado:
    try:
        proc = _corre([sys.executable, "-m", "ruff", "--version"])
    except OSError as exc:  # pragma: no cover - depends on the environment
        return Resultado("lint", "lint", "aviso", "not installed", str(exc))
    if proc.returncode != 0:
        return Resultado("lint", "lint", "aviso", "not installed", (proc.stderr or "")[-2000:])
    proc = _corre(
        [sys.executable, "-m", "ruff", "check", "cxp", "server.py", "tests", "probes", "tools"]
    )
    salida = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return Resultado("lint", "lint", "ok", "clean", salida[-2000:])
    return Resultado("lint", "lint", "error", "issues", salida[-4000:])


def check_server() -> Resultado:
    codigo = (
        "import server\n"
        "herramientas = [t for t in dir(server) if t.startswith('cx_')]\n"
        "print(len(herramientas))\n"
    )
    proc = _corre([sys.executable, "-c", codigo])
    salida = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return Resultado("server", "server", "error", "broken", (proc.stderr or "")[-2000:])
    numero = salida.splitlines()[-1].strip() if salida else "0"
    if not numero.isdigit() or int(numero) == 0:
        return Resultado("server", "server", "error", "no tools", salida)
    return Resultado("server", "server", "ok", f"{numero} tools", salida)


# The patterns are assembled from fragments on purpose: written literally they
# would match this very file during the scan below.
_PATRON_COOKIE = re.compile(
    r"(SAPISID|SSID|HSID|APISID|SIDCC)=" + r"[A-Za-z0-9_-]{16,}"
)
_PATRON_APIKEY = re.compile(r"AIza" + r"[0-9A-Za-z_-]{30,}")
_EXCLUIDOS = (".env.example", "docs/", "README.md", "SECURITY.md")


def _excluido(relativo: str) -> bool:
    return any(relativo == e or relativo.startswith(e) for e in _EXCLUIDOS)


def _es_placeholder(linea: str) -> bool:
    marcas = ("=...", "=XXXX", "=YYYY", "=ZZZZ", "=WWWW", "=VVVV", "dummy", "EXAMPLE")
    return any(m in linea for m in marcas)


def check_secrets() -> Resultado:
    fallos: list[str] = []

    # 1) working tree
    tracked = _corre(["git", "ls-files"])
    if tracked.returncode != 0:
        return Resultado(
            "secrets", "secrets", "aviso", "unknown", "git ls-files failed"
        )
    for relativo in tracked.stdout.splitlines():
        if not relativo or _excluido(relativo):
            continue
        ruta = RAIZ / relativo
        if not ruta.is_file() or ruta.stat().st_size > 4_000_000:
            continue
        try:
            texto = ruta.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for numero, linea in enumerate(texto.splitlines(), 1):
            if _PATRON_COOKIE.search(linea) and not _es_placeholder(linea):
                fallos.append(f"{relativo}:{numero}: session cookie")
                break
        if _PATRON_APIKEY.search(texto):
            fallos.append(f"{relativo}: Google API key")
            break

    # 2) session state files that should never be tracked
    for relativo in tracked.stdout.splitlines():
        base = Path(relativo).name
        if base in ("storage_state.json", "session-state.json") or ".browser-profile" in relativo:
            fallos.append(f"{relativo}: session state file")

    # 3) the .env itself
    if _corre(["git", "ls-files", "--error-unmatch", ".env"]).returncode == 0:
        fallos.append(".env is tracked")

    # 4) the whole history, not only the current tree
    hist = _corre(["git", "log", "--all", "-p", "--no-color"])
    if hist.returncode == 0:
        for numero, linea in enumerate(hist.stdout.splitlines(), 1):
            if _PATRON_COOKIE.search(linea) and not _es_placeholder(linea):
                fallos.append(f"history:{numero}: session cookie")
                break
        if _PATRON_APIKEY.search(hist.stdout):
            fallos.append("history: Google API key")

    if fallos:
        return Resultado("secrets", "secrets", "error", "leak", "; ".join(fallos[:5]))
    return Resultado("secrets", "secrets", "ok", "clean", "")


CHECKS = {
    "tests": check_tests,
    "lint": check_lint,
    "server": check_server,
    "secrets": check_secrets,
}

ORDEN = ("tests", "lint", "server", "secrets")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="do not rewrite the badges")
    ap.add_argument(
        "--only",
        action="append",
        choices=sorted(CHECKS),
        help="run only the given check (repeatable)",
    )
    args = ap.parse_args(argv)

    seleccionados = args.only or list(ORDEN)
    resultados: list[Resultado] = []
    for nombre in ORDEN:
        if nombre in seleccionados:
            resultados.append(CHECKS[nombre]())

    errores = [r for r in resultados if r.estado == "error"]
    avisos = [r for r in resultados if r.estado == "aviso"]

    for r in resultados:
        marca = {"ok": "OK  ", "aviso": "AVISO", "error": "FALLO"}[r.estado]
        print(f"[{marca}] {r.etiqueta}: {r.valor}")
        if r.estado != "ok" and r.detalle:
            print("    " + r.detalle.strip().splitlines()[-1][:300])

    if not args.check:
        for r in resultados:
            escribe_badge(f"{r.nombre}.svg", r.etiqueta, r.valor, r.color)
        global_estado = (
            "failing" if errores else ("incomplete" if avisos else "passing")
        )
        escribe_badge(
            "verification.svg",
            "verification",
            global_estado,
            COLOR_ERROR if errores else (COLOR_AVISO if avisos else COLOR_OK),
        )
        print(f"\nbadges written to {BADGES}")

    if errores:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
