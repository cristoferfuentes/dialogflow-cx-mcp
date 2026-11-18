"""Local proxy that ENDS the TLS (MITM) so the browser can get out.

Why it is needed (measured on this machine):

* `curl` and Python **do** complete TLS with Google (1.8 s). Browsers
  (Chromium **and** Firefox) **do not**: the `TCP connect` completes
  (`is_usable=true`), the browser sends its ClientHello (1,820 B) and the server
  responds (1,757 B remain in the kernel buffer) but the browser **never
  reads** the response -> timeout. Dropped: certificate (curl verifies OK with
  the same one), QUIC, ECH, post-quantum, MTU of the interface, CONNECT proxy,
  Chromium (Firefox fails the same), `/etc/hosts`.

Since the problem is in the browser's **TLS**, the solution is for the browser
**not to do TLS with the internet**: it speaks TLS with this proxy (localhost, which does
work) and the proxy speaks TLS with Google using Python (which does work).

The proxy:
1. accepts `CONNECT host:443`;
2. opens the TLS tunnel against the real host (with MSS clamp);
3. terminates the client's TLS with a local certificate;
4. forwards requests/responses in HTTP/1.1;
5. injects `Authorization: SAPISIDHASH ...` toward the console backend,
   computed with the `__Secure-3PSID` cookie that the browser itself sends (that
   cookie is HttpOnly: only the proxy can see it)."""
from __future__ import annotations

import hashlib
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

_DEBUG = os.environ.get("MITM_DEBUG", "") not in ("", "0", "false")


def _log(msg: str) -> None:
    if _DEBUG:
        print(f"[mitm] {msg}", flush=True)

MSS = 1200
BUF = 65536
# No header is removed on forwarding. Measured: removing `connection`
# toward the upstream caused the 2nd request on the same connection (the redirect
# to /ServiceLogin of the Google login) to be sent but the server never
# responded -> navigation hung. The request is forwarded as
# is, only the authorization headers are ADD on it.
HOP_BY_HOP: set[str] = set()


def sapisid_hash(origin: str, sapisid: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"{ts}_{hashlib.sha1(f'{ts} {sapisid} {origin}'.encode()).hexdigest()}"


CA_DIR_NAME = "ca"
CA_TRUST_PATH = Path("/usr/local/share/ca-certificates/cx-playbook-proxy-ca.crt")


def ensure_ca(dirpath: Path) -> tuple[Path, Path]:
    """Create (or reuse) a local CA and install it in the system trust store.

    Without this, the certificate presented by the proxy is self-signed and the
    browser flags the origin as **"Not secure"**: Google requires a secure
    context for login (cookie `__Secure-3PSID`) and gets stuck on the
    email screen.

    With the CA installed in `/usr/local/share/ca-certificates` + `update-ca-certificates`,
    the per-host certificate (with the correct SAN) actually validates: green
    padlock and login works."""
    ca_dir = dirpath / CA_DIR_NAME
    ca_dir.mkdir(parents=True, exist_ok=True)
    key, crt = ca_dir / "ca.key", ca_dir / "ca.crt"
    if not (key.exists() and crt.exists()):
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
                "-out", str(crt), "-days", "3650", "-nodes",
                "-subj", "/CN=cx-playbook Local Proxy CA/O=cx-playbook",
                "-addext", "basicConstraints=critical,CA:TRUE,pathlen:1",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            ],
            capture_output=True, check=True,
        )
        _log(f"CA creada en {crt}")
    _install_ca_in_trust_store(crt)
    return key, crt


def _install_ca_in_trust_store(crt: Path) -> None:
    """Copy the CA to the system trust store and register it (idempotent)."""
    try:
        target = CA_TRUST_PATH
        if target.exists() and target.read_bytes() == crt.read_bytes():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(crt.read_bytes())
        subprocess.run(["update-ca-certificates"], capture_output=True, timeout=120)
        _log(f"CA installed in the trust store: {target}")
    except Exception as exc:
        _log(f"could not install the CA in the trust store: {exc}")


def ensure_certificate(dirpath: Path, hosts: list[str]) -> tuple[Path, Path]:
    """Self-signed local certificate for the hosts we proxy."""
    dirpath.mkdir(parents=True, exist_ok=True)
    key, crt = dirpath / "proxy.key", dirpath / "proxy.crt"
    if key.exists() and crt.exists():
        return key, crt
    san = ",".join(f"DNS:{h}" for h in hosts)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
            "-out", str(crt), "-days", "825", "-nodes", "-subj", "/CN=cx-playbook-local-proxy",
            "-addext", f"subjectAltName={san}",
        ],
        capture_output=True, check=True,
    )
    return key, crt


def certificate_for_host(dirpath: Path, host: str) -> tuple[Path, Path]:
    """Certificate **per host**, signed by the local CA.

    It is essential for login: the corporate SSO redirects to an IdP that
    is not Google's (p.ej. `*.miempresa.cl`) and the proxy must also be able to
    terminate that TLS.

    When the CA signs it (installed in the system trust store), the browser
    actually validates it: the origin becomes **secure** and Google accepts the
    login flow (with a self-signed one it stays at "Not secure")."""
    dirpath = dirpath / "hosts"
    dirpath.mkdir(parents=True, exist_ok=True)
    safe = host.replace("*", "_").replace("/", "_")
    key, crt = dirpath / f"{safe}.key", dirpath / f"{safe}.crt"
    if key.exists() and crt.exists():
        return key, crt
    ca_key, ca_crt = ensure_ca(dirpath.parent)
    cnf = dirpath / f"{safe}.cnf"
    cnf.write_text(
        f"[req]\ndistinguished_name=dn\nreq_extensions=v3\n[dn]\nCN={host}\n"
        f"[v3]\nsubjectAltName=DNS:{host}\nbasicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    csr = dirpath / f"{safe}.csr"
    subprocess.run(
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(csr), "-subj", f"/CN={host}", "-config", str(cnf)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
         "-CAcreateserial", "-out", str(crt), "-days", "825", "-extfile", str(cnf),
         "-extensions", "v3"],
        capture_output=True, check=True,
    )
    return key, crt


def _upstream_tls(host: str, port: int, timeout: float = 45.0) -> ssl.SSLSocket:
    """Open the TLS tunnel against the real host.

    The MSS is set **BEFORE** from connect: afterwards it is useless (the MSS was
    already negotiated in the SYN). With `TCP_MAXSEG` set beforehand, the SYN advertises 1200 and
    the segments are not fragmented in the ESP tunnel."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])  # HTTP/1.1: allows injecting headers
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, MSS)
    except OSError:
        pass
    try:
        raw.connect((host, port))
    except OSError:
        raw.close()
        # Retry without the clamp (in case the previous setsockopt breaks the SYN).
        raw = socket.create_connection((host, port), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=host)


def _inject_auth(
    data: bytes,
    host: str,
    quota_project: str,
    bearer_token: str | None = None,
    bearer_hosts: set[str] | None = None,
) -> bytes:
    """Add `Authorization` to browser requests.

    Priority:
      1. `SAPISIDHASH` if the browser has a Google session cookie
         (first part / UDS, which is what the console actually uses).
      2. `Bearer <token ADC>` for console backend hosts if there is no
         session cookie. This way the console can be automated WITHOUT Google
         login: `dialogflow.clients6.google.com` also accepts the standard
         OAuth token (measured: GET of agents in 1.07 s).

    Only touches the package that **opens** a HTTP request (request-line validates).
    A body that starts with "GET " is not mistaken for a new request."""
    if not data.startswith((b"GET ", b"POST ", b"PATCH ", b"PUT ", b"DELETE ", b"HEAD ")):
        return data
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        return data
    lines = head.split(b"\r\n")
    # The request-line must be "<METHOD> <path> HTTP/1.1"; otherwise it is a body.
    parts = lines[0].split()
    if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
        return data
    # The CORS preflight (OPTIONS) does NOT authenticate: if a
    # `Authorization` the browser did not request is added, the browser rejects the
    # preflight response and the XHR is never sent (measured: the console
    # stays blank when requesting the agent).
    if parts[0].upper() == b"OPTIONS":
        return data
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.decode().strip().lower()] = v.decode().strip()
    if headers.get("authorization"):
        return data  # already authenticated: left untouched
    sapisid = None
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k in ("__Secure-3PSID", "SAPISID", "__Secure-1PSID") and v:
            sapisid = v
            break
    auth_value = None
    if sapisid:
        origin = f"https://{host}"
        auth_value = f"SAPISIDHASH {sapisid_hash(origin, sapisid)}"
    elif bearer_token and (not bearer_hosts or host in bearer_hosts):
        auth_value = f"Bearer {bearer_token}"
    if not auth_value:
        return data
    out = [lines[0]]
    for k, v in headers.items():
        if k not in HOP_BY_HOP:
            out.append(f"{k}: {v}".encode())
    out.append(f"Authorization: {auth_value}".encode())
    out.append(f"x-goog-user-project: {quota_project}".encode())
    _log(f"   auth={auth_value.split(' ')[0]} cookies={headers.get('cookie','')[:120]!r}")
    return b"\r\n".join(out) + b"\r\n\r\n" + rest


class MitmProxy:
    """Local MITM proxy over raw sockets (starts on a thread)."""

    def __init__(
        self,
        cert_dir: Path,
        hosts: list[str],
        host: str = "127.0.0.1",
        port: int = 0,
        quota_project: str = "",
        bearer_token: str | None = None,
        bearer_hosts: set[str] | None = None,
    ):
        self.key, self.crt = ensure_certificate(cert_dir, hosts)
        self.cert_dir = cert_dir
        self.quota_project = quota_project
        self.bearer_token = bearer_token
        self.bearer_hosts = bearer_hosts or set()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(64)
        self.host, self.port = self._srv.getsockname()
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> MitmProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ------------------------------------------------------------- internal
    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        upstream = None
        host = "?"
        try:
            conn.settimeout(30)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            line = data.split(b"\r\n")[0]
            if not line.upper().startswith(b"CONNECT"):
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, _, port = line.split(b" ")[1].decode().partition(":")
            port = int(port or 443)

            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _log(f"CONNECT {host}:{port}")

            # Per-host certificate: this way the corporate SSO (non-Google IdP) can also do TLS through the proxy.
            key, crt = certificate_for_host(self.cert_dir, host)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(str(crt), str(key))
            # Explicit ALPN: the browser must speak HTTP/1.1 to the proxy
            # (otherwise it tries h2 and the HTTP/1.1 relay does not understand it).
            try:
                server_ctx.set_alpn_protocols(["http/1.1"])
            except NotImplementedError:
                pass
            client_tls = server_ctx.wrap_socket(conn, server_side=True)
            client_tls.settimeout(None)
            _log(f"tls cliente ok {host} alpn={client_tls.selected_alpn_protocol()}")

            upstream = _upstream_tls(host, port)
            upstream.settimeout(None)
            _log(f"tls upstream ok {host} alpn={upstream.selected_alpn_protocol()}")
            self._relay(client_tls, upstream, host)
        except (OSError, ssl.SSLError, ValueError) as exc:
            # It does NOT swallow silently: if the TLS with the upstream fails, the
            # browser request hangs with no explanation.
            _log(f"ERROR en {host}: {type(exc).__name__}: {exc}")
        finally:
            for s in (conn, upstream):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    def _relay(self, client: ssl.SSLSocket, upstream: ssl.SSLSocket, host: str):
        """Bidirectional relay: **one thread per direction**, blocking sockets.

        Why two threads and not `select`:
        with non-blocking sockets + `select` there are three ways to lose bytes
        (measured: the 2nd HTTP response arrived corrupted, `00000001` instead of the
        status line):
          * `select` says "no legible" but the TLS socket already has decrypted
            data in its internal buffer (typical with GRO active);
          * `SSLWantReadError` on `recv` swallows a half-read TLS record;
          * partial `send()` leaves data in a `pending` that `select` (which only
            expected a read) does not wake again.
        With **blocking** `recv`/`sendall` and one thread per direction, order and
        integrity are guaranteed by the kernel. Also `sendall` does not return
        until everything has been written."""
        client.settimeout(120)
        upstream.settimeout(120)
        counters = {"c2u": 0, "u2c": 0}

        def pump(src: ssl.SSLSocket, dst: ssl.SSLSocket, inject: bool, key: str):
            try:
                while True:
                    try:
                        data = src.recv(BUF)
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue
                    except TimeoutError:
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    if inject:
                        data = _inject_auth(
                            data, host, self.quota_project,
                            self.bearer_token, self.bearer_hosts,
                        )
                        if data[:3] in (b"GET", b"POS", b"HEA", b"PUT", b"DEL"):
                            parts = data.split(b" ")
                            _log(f"-> {parts[0].decode(errors='replace')} {parts[1].decode(errors='replace')[:110]}")
                            _log("   RAW REQUEST:\n" + data[:1200].decode(errors="replace"))
                    else:
                        if data.startswith(b"HTTP/"):
                            _log(f"<- {data.split(chr(13).encode())[0].decode(errors='replace')[:120]} ({len(data)} B)")
                            _log("   RAW RESPONSE:\n" + data[:1200].decode(errors="replace"))
                    counters[key] += len(data)
                    try:
                        dst.sendall(data)
                    except OSError:
                        break
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t_up = threading.Thread(target=pump, args=(client, upstream, True, "c2u"), daemon=True)
        t_down = threading.Thread(target=pump, args=(upstream, client, False, "u2c"), daemon=True)
        t_up.start()
        t_down.start()
        t_up.join()
        t_down.join()
        _log(f"relay {host}: navegador->google {counters['c2u']} B | google->navegador {counters['u2c']} B")
