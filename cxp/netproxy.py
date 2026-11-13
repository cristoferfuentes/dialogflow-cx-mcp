"""Proxy HTTP local (CONNECT) con clamp de MSS.

Por que hace falta (medido en este entorno, ver 04-RED-Y-LATENCIA.md):

* Chromium **no** puede hablar TLS con ningun host externo: su ClientHello
  ocupa **1.729 B** y no llega. El `TCP connect` si se completa
  (`TCP_CONNECT_JOB_VERIFY_IP_ENDPOINT_USABLE: is_usable=true`), pero el
  servidor nunca responde al handshake TLS -> timeout.
* Bajar el MTU de la interfaz **no** lo arregla (probado con 1400/1200/1000).
* Chromium fija su propio MSS, asi que no se puede clampar por socket.
* En cambio, **HTTP plano funciona** (0,6 s) y **localhost funciona** siempre.

Solucion: Chromium habla con un proxy local (localhost) y el proxy es el que
sale a internet con un socket cuyo MSS si se puede clampar (TCP_MAXSEG=1200
despues del connect, que es lo que si funciona).

El proxy solo hace `CONNECT` (TLS tunneling) y reenvia bytes en ambos
sentidos; no inspecciona ni descifra nada.
"""
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
        # El clamp DESPUES del connect: antes rompe la conexion (BrokenPipe).
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, MSS)
    except OSError:
        pass
    raw.settimeout(None)
    return raw


class _Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silencia el log por conexion
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
    """Proxy CONNECT local. Arranca en un hilo y expone el puerto."""

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
