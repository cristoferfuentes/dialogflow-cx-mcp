"""Cliente HTTP con las lecciones de red del workspace aplicadas (2026-11-06).

Estado REAL de la red en este host (medido, no supuesto):

1. **MTU de la interfaz = 900** (no 1500) y **TSO/GSO/GRO apagados**
   (`ethtool -k eth0`): esa es la mitigacion que esta aplicada a nivel de
   sistema. El clamp de MSS del socket queda por encima del MTU de la ruta y,
   ademas, se aplica DESPUES del handshake (antes rompe la conexion:
   BrokenPipe medido) -> hoy es inerte; la proteccion real es el MTU.
2. **DNS con IPs fijadas en `/etc/hosts`**: `dialogflow.clients6.google.com`
   apunta a 7 IPs. Si UNA se pone negra, `create_connection` la prueba con el
   timeout de socket -> por eso el **timeout de connect es corto (8 s)**: se
   cae rapido a la siguiente IP en vez de esperar 45 s. Ese era el defecto de
   "1,6 s o 138 s" que describia la version anterior de este modulo.
3. **Conexion TLS reutilizada por hilo** (`threading.local`): el servidor MCP
   hace las llamadas en un solo hilo, asi que el handshake (0,66 s medido con
   curl) se paga UNA vez, no en cada peticion.
4. **Timeout de lectura = 45 s**; la peticion se re-firma (`SAPISIDHASH`) en
   cada llamada, asi que no hay que refrescar nada.

Ademas: solo se reintenta un error de RED (no un HTTP), una vez.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class NetworkError(RuntimeError):
    """Error de red: reintentable."""


class HttpStatusError(RuntimeError):
    """Error del servidor: NO se reintenta a ciegas."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Conexion TLS con timeout de connect corto y clamp de MSS.

    El clamp se aplica DESPUES del `connect()` (y por tanto despues del
    handshake TLS): fijarlo antes del `connect()` rompe la conexion
    (BrokenPipe medido). Con el MTU de la interfaz en 900 el kernel ya limita
    los segmentos, asi que el clamp es una red de seguridad, no la proteccion
    principal.

    El timeout de connect se separa del de lectura: con las IPs fijadas en
    `/etc/hosts`, una IP negra no puede costar 45 s de espera (se prueba la
    siguiente IP en 8 s).
    """

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
        # Tras el connect el socket se queda con el timeout de LECTURA.
        if self.sock and read_timeout:
            try:
                self.sock.settimeout(read_timeout)
            except OSError:
                pass


class Session:
    """Sesion HTTP con keep-alive, MSS clamp y reintento solo de red."""

    def __init__(self, timeout: float = 45.0, maxseg: int = 1200, retries: int = 1,
                 connect_timeout: float | None = 8.0):
        self.timeout = timeout
        self.maxseg = maxseg
        self.retries = retries
        self.connect_timeout = connect_timeout
        self._local = threading.local()
        # Cronometria por peticion (diagnostico): connect, espera de cabeceras
        # y cuerpo. Sirve para separar "tarda la red" de "tarda el backend".
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
