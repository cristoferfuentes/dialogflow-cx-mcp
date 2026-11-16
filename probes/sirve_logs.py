#!/usr/bin/env python3
"""Minimal static server to publish the conversation logs (temporary channel).

It only serves the given directory and ONLY what is explicitly asked for:
no directory listing, no hidden files, and with a generated index.

Usage:
    .venv/bin/python probes/sirve_logs.py --raiz <dir> --puerto 8099
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
from pathlib import Path

# Where the logs live. Override with CXP_LOGS_DIR.
RAIZ = Path(os.environ.get(
    "CXP_LOGS_DIR",
    str(Path(__file__).resolve().parent.parent / "logs-conversaciones"),
)).resolve()
PUERTO = 8099


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves only the files of the RAIZ directory, without listing directories."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(RAIZ), **kw)

    def list_directory(self, path):        # no directory listing
        self.send_error(403, "Directory not allowed")
        return None

    def do_GET(self):                     # blocks dotfiles and TV
        limpio = self.path.split("?")[0]
        if any(seg.startswith(".") for seg in limpio.split("/") if seg):
            self.send_error(403, "Forbidden")
            return
        # MEASURED (2026-11-16): `/` gave 403 because `list_directory` is
        # blocked and `SimpleHTTPRequestHandler` does NOT serve `index.html` alone.
        # The root is redirected to the explicit index (the listing stays forbidden).
        if limpio in ("", "/"):
            self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self):
        limpio = self.path.split("?")[0]
        if limpio in ("", "/"):
            self.path = "/index.html"
        super().do_HEAD()

    def do_POST(self):
        self.send_error(405, "Read only")

    def send_head(self):
        """Supports `Range` so that big files can be downloaded/resumed."""
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            self.send_error(403, "Directory not allowed")
            return None
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                start_s, end_s = rng[6:].split("-")
                start = int(start_s)
                end = int(end_s) if end_s else None
            except ValueError:
                self.send_error(400, "Invalid Range")
                return None
            size = os.path.getsize(path)
            if start >= size:
                self.send_error(416, "Range out of bounds")
                return None
            if end is None or end >= size:
                end = size - 1
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as fh:
                fh.seek(start)
                self.copyfile(fh, self.wfile)
            return None
        return super().send_head()

    def log_message(self, fmt, *args):
        print("[logs] " + fmt % args, flush=True)


def main() -> int:
    global RAIZ, PUERTO
    ap = argparse.ArgumentParser()
    ap.add_argument("--raiz", default=str(RAIZ))
    ap.add_argument("--puerto", type=int, default=PUERTO)
    args = ap.parse_args()
    RAIZ = Path(args.raiz).resolve()
    PUERTO = args.puerto

    os.chdir(RAIZ)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PUERTO), Handler) as httpd:
        print(f"[logs] serving {RAIZ} on 127.0.0.1:{PUERTO}", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
