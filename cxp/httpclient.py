"""Client HTTP with the workspace network lessons applied (2026-11-06).

State REAL of the network on this host (measured, not assumed):

1. **MTU of the interface = 900** (not 1500) and **TSO/GSO/GRO off**
   (`ethtool -k eth0`): that is the mitigation applied at the
   system level. The MSS clamp of the socket sits above the MTU of the route and,
   moreover, is applied AFTER of the handshake (before it breaks the connection:
   BrokenPipe measured) -> today it is inert; the real protection is the MTU.
2. **DNS with IPs pinned in `/etc/hosts`**: `dialogflow.clients6.google.com`
   points to 7 IPs. If ONE goes black, `create_connection` tests it with the
   socket timeout -> that is why the **connect timeout is short (8 s)**: it
   falls quickly to the next IP instead of waiting 45 s. That was the defect of
   "1,6 s o 138 s" described by the previous version of this module.
3. **TLS connection reused per thread** (`threading.local`): the MCP server
   makes the calls in a single thread, so the handshake (0.66 s measured with
   curl) is paid ONCE, not on every request.
4. **Read timeout = 45 s**; the request is re-firma (`SAPISIDHASH`) on
   every call, so there is nothing to refresh.

Also: only a NETWORK error is retried (not an HTTP), once."""
from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class NetworkError(RuntimeError):
    """Network error: retryable."""


class HttpStatusError(RuntimeError):
    """Server error: NO blind retry."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection with a short connect timeout and a MSS clamp.

    The clamp is applied AFTER the `connect()` (and therefore after the TLS
    handshake): setting it before the `connect()` breaks the connection
    (BrokenPipe measured). With the interface MTU at 900 the kernel already limits
    the segments, so the clamp is a safety net, not the main
    protection.

    The connect timeout is separated from the read timeout: with the IPs fixed in
    `/etc/hosts`, a black IP cannot cost 45 s of waiting (the
    next IP is tried in 8 s)."""

    def __init__(self, host: str, port: int, timeout: float, maxseg: int,
                 connect_timeout: float | None = None):
        super().__init__(host, port, timeout=timeout)
        self._maxseg = maxseg
        self._connect_timeout = connect_timeout

    def connect(self) -> None:
        read_timeout = self.timeout
        if self._connect_timeout:
            self.timeout = self._connect_timeout
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, self._maxseg)
        except (OSError, AttributeError):
            pass
        # After connect the socket keeps the READ timeout.
        if self.sock and read_timeout:
            try:
                self.sock.settimeout(read_timeout)
            except OSError:
                pass


class Session:
    """Session HTTP with keep-alive, MSS clamp and network-only retry."""

    def __init__(self, timeout: float = 45.0, maxseg: int = 1200, retries: int = 1,
                 connect_timeout: float | None = 8.0):
        self.timeout = timeout
        self.maxseg = maxseg
        self.retries = retries
        self.connect_timeout = connect_timeout
        self._local = threading.local()
        # Per-request timing (diagnostics): connect, header wait
        # and body. Used to separate "the network is slow" from "the backend is slow".
        self._stats_lock = threading.Lock()
        self._stats: list[dict] = []
        self.last_stats: list[dict] = []

    def _stat(self, row: dict) -> None:
        with self._stats_lock:
            self._stats.append(row)

    def take_stats(self) -> list[dict]:
        with self._stats_lock:
            rows, self._stats = self._stats, []
        return rows

    def _conn(self, host: str, port: int) -> _PinnedHTTPSConnection:
        cache = getattr(self._local, "conns", None)
        if cache is None:
            cache = self._local.conns = {}
        key = (host, port)
        conn = cache.get(key)
        if conn is None:
            conn = cache[key] = _PinnedHTTPSConnection(
                host, port, self.timeout, self.maxseg, self.connect_timeout)
        return conn

    def _drop(self, host: str, port: int) -> None:
        cache = getattr(self._local, "conns", None) or {}
        conn = cache.pop((host, port), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if isinstance(body, str):
            body = body.encode("utf-8")

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            before = getattr(self._local, "conns", None) or {}
            reused = (host, port) in before
            conn = self._conn(host, port)
            if timeout is not None:
                conn.timeout = timeout
            t0 = time.perf_counter()
            try:
                conn.request(method, path, body=body, headers=headers or {})
                t_send = time.perf_counter()
                resp = conn.getresponse()
                t_head = time.perf_counter()
                text = resp.read().decode("utf-8", errors="replace")
                t_end = time.perf_counter()
                if resp.will_close:
                    self._drop(host, port)
                self._stat({
                    "method": method, "host": host, "path": path[:120],
                    "reutilizada": reused, "bytes": len(text),
                    "send_ms": round((t_send - t0) * 1000, 1),
                    "cabeceras_ms": round((t_head - t_send) * 1000, 1),
                    "cuerpo_ms": round((t_end - t_head) * 1000, 1),
                    "total_ms": round((t_end - t0) * 1000, 1),
                    "will_close": bool(resp.will_close),
                })
                return resp.status, text
            except TimeoutError as exc:
                last_exc = exc
                self._drop(host, port)
            except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
                last_exc = exc
                self._drop(host, port)
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise NetworkError(f"RED: {type(last_exc).__name__}: {last_exc}")

    def json(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        payload: object | None = None,
        timeout: float | None = None,
    ) -> object:
        import json as _json

        hdrs = dict(headers or {})
        body = None
        if payload is not None:
            body = _json.dumps(payload).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        status, text = self.request(method, url, hdrs, body, timeout=timeout)
        try:
            data = _json.loads(text) if text else {}
        except ValueError:
            data = {"_raw": text}
        if status >= 400:
            raise HttpStatusError(status, text)
        return data
