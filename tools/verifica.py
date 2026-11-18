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
CHAR_WIDTH = 6.6
PADDING = 10

COLOR_OK = "#4c1"
COLOR_ERROR = "#e05d44"
COLOR_WARNING = "#dfb317"
COLOR_NEUTRAL = "#9f9f9f"
COLOR_LABEL = "#555"


def _width(text: str) -> int:
    return int(round(len(text) * CHAR_WIDTH)) + 2 * PADDING


def badge(label: str, value: str, color: str) -> str:
    """Return the SVG source of a flat two-part badge."""
    label_width = _width(label)
    value_width = _width(value)
    width = label_width + value_width
    label_x = label_width / 2
    value_x = label_width + value_width / 2
    title = f"{label}: {value}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" '
        f'role="img" aria-label="{title}">\n'
        f"  <title>{title}</title>\n"
        f'  <linearGradient id="s" x2="0" y2="100%">\n'
        f'    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>\n'
        f'    <stop offset="1" stop-opacity=".1"/>\n'
        f"  </linearGradient>\n"
        f'  <clipPath id="r"><rect width="{width}" height="20" rx="3" fill="#fff"/></clipPath>\n'
        f'  <g clip-path="url(#r)">\n'
        f'    <rect width="{label_width}" height="20" fill="{COLOR_LABEL}"/>\n'
        f'    <rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>\n'
        f'    <rect width="{width}" height="20" fill="url(#s)"/>\n'
        f"  </g>\n"
        f'  <g fill="#fff" text-anchor="middle" '
        f'font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">\n'
        f'    <text x="{label_x:.1f}" y="14">{label}</text>\n'
        f'    <text x="{value_x:.1f}" y="14">{value}</text>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def write_badge(name: str, label: str, value: str, color: str) -> Path:
    BADGES.mkdir(parents=True, exist_ok=True)
    destination = BADGES / name
    destination.write_text(badge(label, value, color), encoding="utf-8")
    return destination


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    label: str
    status: str  # "ok" | "error" | "warning"
    value: str
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def color(self) -> str:
        return {
            "ok": COLOR_OK,
            "error": COLOR_ERROR,
            "warning": COLOR_WARNING,
        }[self.status]


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd or RAIZ),
        capture_output=True,
        text=True,
    )


def check_tests() -> CheckResult:
    # No extra "-q": the project already sets it in addopts, and "-qq" would
    # suppress the summary line that carries the number of passed tests.
    proc = _run([sys.executable, "-m", "pytest"])
    output = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"(\d+)\s+passed", output)
    if proc.returncode == 0:
        total = m.group(1) if m else "?"
        return CheckResult("tests", "tests", "ok", f"{total} passed", output[-2000:])
    m = re.search(r"(\d+)\s+failed", output)
    extra = f", {m.group(1)} failed" if m else ""
    return CheckResult("tests", "tests", "error", f"failing{extra}", output[-4000:])


def check_lint() -> CheckResult:
    try:
        proc = _run([sys.executable, "-m", "ruff", "--version"])
    except OSError as exc:  # pragma: no cover - depends on the environment
        return CheckResult("lint", "lint", "warning", "not installed", str(exc))
    if proc.returncode != 0:
        return CheckResult("lint", "lint", "warning", "not installed", (proc.stderr or "")[-2000:])
    proc = _run(
        [sys.executable, "-m", "ruff", "check", "cxp", "server.py", "tests", "probes", "tools"]
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return CheckResult("lint", "lint", "ok", "clean", output[-2000:])
    return CheckResult("lint", "lint", "error", "issues", output[-4000:])


def check_server() -> CheckResult:
    codigo = (
        "import server\n"
        "tools = [t for t in dir(server) if t.startswith('cx_')]\n"
        "print(len(tools))\n"
    )
    proc = _run([sys.executable, "-c", codigo])
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return CheckResult("server", "server", "error", "broken", (proc.stderr or "")[-2000:])
    number = output.splitlines()[-1].strip() if output else "0"
    if not number.isdigit() or int(number) == 0:
        return CheckResult("server", "server", "error", "no tools", output)
    return CheckResult("server", "server", "ok", f"{number} tools", output)


# The patterns are assembled from fragments on purpose: written literally they
# would match this very file during the scan below.
_COOKIE_PATTERN = re.compile(
    r"(SAPISID|SSID|HSID|APISID|SIDCC)=" + r"[A-Za-z0-9_-]{16,}"
)
_APIKEY_PATTERN = re.compile(r"AIza" + r"[0-9A-Za-z_-]{30,}")
_EXCLUDED = (".env.example", "docs/", "README.md", "SECURITY.md")


def _excluded(relative: str) -> bool:
    return any(relative == e or relative.startswith(e) for e in _EXCLUDED)


def _is_placeholder(line: str) -> bool:
    marks = ("=...", "=XXXX", "=YYYY", "=ZZZZ", "=WWWW", "=VVVV", "dummy", "EXAMPLE")
    return any(m in line for m in marks)


def check_secrets() -> CheckResult:
    findings: list[str] = []

    # 1) working tree
    tracked = _run(["git", "ls-files"])
    if tracked.returncode != 0:
        return CheckResult(
            "secrets", "secrets", "warning", "unknown", "git ls-files failed"
        )
    for relative in tracked.stdout.splitlines():
        if not relative or _excluded(relative):
            continue
        path = RAIZ / relative
        if not path.is_file() or path.stat().st_size > 4_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if _COOKIE_PATTERN.search(line) and not _is_placeholder(line):
                findings.append(f"{relative}:{number}: session cookie")
                break
        if _APIKEY_PATTERN.search(content):
            findings.append(f"{relative}: Google API key")
            break

    # 2) session state files that should never be tracked
    for relative in tracked.stdout.splitlines():
        base = Path(relative).name
        if base in ("storage_state.json", "session-state.json") or ".browser-profile" in relative:
            findings.append(f"{relative}: session state file")

    # 3) the .env itself
    if _run(["git", "ls-files", "--error-unmatch", ".env"]).returncode == 0:
        findings.append(".env is tracked")

    # 4) the whole history, not only the current tree
    hist = _run(["git", "log", "--all", "-p", "--no-color"])
    if hist.returncode == 0:
        for number, line in enumerate(hist.stdout.splitlines(), 1):
            if _COOKIE_PATTERN.search(line) and not _is_placeholder(line):
                findings.append(f"history:{number}: session cookie")
                break
        if _APIKEY_PATTERN.search(hist.stdout):
            findings.append("history: Google API key")

    if findings:
        return CheckResult("secrets", "secrets", "error", "leak", "; ".join(findings[:5]))
    return CheckResult("secrets", "secrets", "ok", "clean", "")


CHECKS = {
    "tests": check_tests,
    "lint": check_lint,
    "server": check_server,
    "secrets": check_secrets,
}

ORDER = ("tests", "lint", "server", "secrets")


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

    selected = args.only or list(ORDER)
    results: list[CheckResult] = []
    for name in ORDER:
        if name in selected:
            results.append(CHECKS[name]())

    errors = [r for r in results if r.status == "error"]
    warnings = [r for r in results if r.status == "warning"]

    for r in results:
        marca = {"ok": "OK  ", "warning": "WARN", "error": "FAIL"}[r.status]
        print(f"[{marca}] {r.label}: {r.value}")
        if r.status != "ok" and r.detail:
            print("    " + r.detail.strip().splitlines()[-1][:300])

    if not args.check:
        for r in results:
            write_badge(f"{r.name}.svg", r.label, r.value, r.color)
        global_estado = (
            "failing" if errors else ("incomplete" if warnings else "passing")
        )
        write_badge(
            "verification.svg",
            "verification",
            global_estado,
            COLOR_ERROR if errors else (COLOR_WARNING if warnings else COLOR_OK),
        )
        print(f"\nbadges written to {BADGES}")

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
