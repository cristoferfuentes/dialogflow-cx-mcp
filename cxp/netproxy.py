"""Local proxy (CONNECT) with MSS clamp.

Why it is needed (measured in this environment, see 04-RED-Y-LATENCY.md):

* Chromium **cannot** speak TLS with any external host: its ClientHello
  takes **1,729 B** and does not get through. The `TCP connect` does complete
  (`TCP_CONNECT_JOB_VERIFY_IP_ENDPOINT_USABLE: is_usable=true`), but the
  server never responds to the TLS handshake -> timeout.
* Lowering the interface MTU **does not** fix it (tested with 1400/1200/1000).
* Chromium sets its own MSS, so it cannot be clamped per socket.
* By contrast, **plain HTTP works** (0.6 s) and **localhost always works**.

Solution: Chromium talks to a local proxy (localhost) and the proxy is what
goes out to the internet with a socket whose MSS can be clamped (TCP_MAXSEG=1200
after the connect, which is what does work).

The proxy only does `CONNECT` (TLS tunneling) and forwards bytes in both
directions; it does not inspect or decrypt anything."""
from __future__ import annotations

import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MSS = 1200
BUF = 65536


def _open_tunnel(host: str, port: int, timeout: float = 45.0) -> socket.socket:
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    raw.connect((host, port))
    try:
        # The AFTER clamp of connect: it breaks the connection first (BrokenPipe).
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, MSS)
    except OSError:
        pass
    raw.settimeout(None)
    return raw


class _Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silences the log per connection
        pass

    def do_CONNECT(self):  # noqa: N802
        host, _, port = self.path.partition(":")
        try:
            upstream = _open_tunnel(host, int(port or 443))
        except OSError as exc:
            self.send_error(502, f"upstream fail: {exc}")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._relay(upstream)

    def _relay(self, upstream: socket.socket):
        client = self.connection
        client.setblocking(False)
        upstream.setblocking(False)
        socks = [client, upstream]
        try:
            while True:
                readable, _, errored = select.select(socks, [], socks, 60)
                if errored:
                    break
                if not readable:
                    continue
                for sock in readable:
                    other = upstream if sock is client else client
                    try:
                        data = sock.recv(BUF)
                    except (BlockingIOError, ssl.SSLWantReadError):
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class LocalProxy:
    """Local CONNECT proxy. Starts on a thread and exposes the port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._server = ThreadingHTTPServer((host, port), _Proxy)
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> LocalProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"
