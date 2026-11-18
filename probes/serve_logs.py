#!/usr/bin/env python3
"""Minimal static server to publish the conversation logs (temporary channel).

It only serves the given directory and ONLY what is explicitly asked for:
no directory listing, no hidden files, and with a generated index.

Usage:
    .venv/bin/python probes/serve_logs.py --root <dir> --port 8099
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
from pathlib import Path

# Where the logs live. Override with CXP_LOGS_DIR.
ROOT = Path(os.environ.get(
    "CXP_LOGS_DIR",
    str(Path(__file__).resolve().parent.parent / "logs-conversaciones"),
)).resolve()
PORT = 8099


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves only the files of the ROOT directory, without listing directories."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def list_directory(self, path):        # no directory listing
        self.send_error(403, "Directory not allowed")
        return None

    def do_GET(self):                     # blocks dotfiles and TV
        clean_path = self.path.split("?")[0]
        if any(seg.startswith(".") for seg in clean_path.split("/") if seg):
            self.send_error(403, "Forbidden")
            return
        # MEASURED (2026-11-16): `/` gave 403 because `list_directory` is
        # blocked and `SimpleHTTPRequestHandler` does NOT serve `index.html` alone.
        # The root is redirected to the explicit index (the listing stays forbidden).
        if clean_path in ("", "/"):
            self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ("", "/"):
            self.path = "/index.html"
        super().do_HEAD()

    def do_POST(self):
        self.send_error(405, "Read only")

    def send_head(self):
        """Supports `Range` so that big files can be downloaded/resumed."""
        route = self.translate_path(self.path)
        if os.path.isdir(route):
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
            size = os.path.getsize(route)
            if start >= size:
                self.send_error(416, "Range out of bounds")
                return None
            if end is None or end >= size:
                end = size - 1
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(route))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(route, "rb") as fh:
                fh.seek(start)
                self.copyfile(fh, self.wfile)
            return None
        return super().send_head()

    def log_message(self, fmt, *args):
        print("[logs] " + fmt % args, flush=True)


def main() -> int:
    global ROOT, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", dest="root", default=str(ROOT))
    ap.add_argument("--port", dest="port", type=int, default=PORT)
    args = ap.parse_args()
    ROOT = Path(args.root).resolve()
    PORT = args.port

    os.chdir(ROOT)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"[logs] serving {ROOT} on 127.0.0.1:{PORT}", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
