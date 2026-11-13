"""Proxy local que TERMINA el TLS (MITM) para que el navegador pueda salir.

Por que hace falta (medido en esta maquina):

* `curl` y Python **si** hacen TLS con Google (1,8 s). Los navegadores
  (Chromium **y** Firefox) **no**: el `TCP connect` se completa
  (`is_usable=true`), el navegador envia su ClientHello (1.820 B) y el servidor
  responde (1.757 B quedan en el buffer del kernel) pero el navegador **nunca
  lee** la respuesta -> timeout. Descartado: certificado (curl verifica OK con
  el mismo), QUIC, ECH, post-quantum, MTU de la interfaz, proxy CONNECT,
  Chromium (Firefox falla igual), `/etc/hosts`.

Como el problema es del **TLS del navegador**, la solucion es que el navegador
**no haga TLS con internet**: habla TLS con este proxy (localhost, que si
funciona) y el proxy habla TLS con Google usando Python (que si funciona).

El proxy:
1. acepta `CONNECT host:443`;
2. abre el tunel TLS contra el host real (con clamp de MSS);
3. termina el TLS del cliente con un certificado local;
4. reenvia peticiones/respuestas en HTTP/1.1;
5. inyecta `Authorization: SAPISIDHASH ...` hacia el backend de la consola,
   calculada con la cookie `__Secure-3PSID` que envia el propio navegador (esa
   cookie es HttpOnly: solo el proxy puede verla).
"""
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
# NO se elimina ninguna cabecera al reenviar. Medido: quitar `connection`
# hacia el upstream hacia que la 2ª peticion de la misma conexion (el redirect
# a /ServiceLogin del login de Google) se enviara pero el servidor no
# respondiera nunca -> la navegacion se colgaba. La peticion se reenvia tal
# cual, solo se le AÑADEN las cabeceras de autorizacion.
HOP_BY_HOP: set[str] = set()


def sapisid_hash(origin: str, sapisid: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f"{ts}_{hashlib.sha1(f'{ts} {sapisid} {origin}'.encode()).hexdigest()}"


CA_DIR_NAME = "ca"
CA_TRUST_PATH = Path("/usr/local/share/ca-certificates/cx-playbook-proxy-ca.crt")


def ensure_ca(dirpath: Path) -> tuple[Path, Path]:
    """Crea (o reutiliza) una CA local y la instala en el trust store del sistema.

    Sin esto, el certificado que presenta el proxy es un autofirmado y el
    navegador marca el origen como **"Not secure"**: Google exige un contexto
    seguro para el login (cookie `__Secure-3PSID`) y se queda pegado en la
    pantalla del correo.

    Con la CA instalada en `/usr/local/share/ca-certificates` + `update-ca-certificates`,
    el certificado por host (con el SAN correcto) valida de verdad: candado
    verde y el login funciona.
    """
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
    """Copia la CA al trust store del sistema y la registra (idempotente)."""
    try:
        target = CA_TRUST_PATH
        if target.exists() and target.read_bytes() == crt.read_bytes():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(crt.read_bytes())
        subprocess.run(["update-ca-certificates"], capture_output=True, timeout=120)
        _log(f"CA instalada en el trust store: {target}")
    except Exception as exc:
        _log(f"no se pudo instalar la CA en el trust store: {exc}")


def ensure_certificate(dirpath: Path, hosts: list[str]) -> tuple[Path, Path]:
    """Certificado local autofirmado para los hosts que proxieamos."""
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
    """Certificado **por host**, firmado por la CA local.

    Es imprescindible para el login: el SSO corporativo redirige a un IdP que
    no es de Google (p.ej. `*.miempresa.cl`) y el proxy tiene que poder
    terminar tambien ese TLS.

    Al firmarlo la CA (instalada en el trust store del sistema), el navegador
    lo valida de verdad: el origen pasa a ser **seguro** y Google acepta el
    flujo de login (con un autofirmado se queda en "Not secure").
    """
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
    """Abre el tunel TLS contra el host real.

    El MSS se fija **ANTES** del connect: despues no sirve (el MSS ya se
    negocio en el SYN). Con `TCP_MAXSEG` puesto antes, el SYN anuncia 1200 y
    los segmentos no se fragmentan en el tunel ESP.
    """
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])  # HTTP/1.1: permite inyectar cabeceras
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
        # Reintento sin el clamp (por si el setsockopt previo rompe el SYN).
        raw = socket.create_connection((host, port), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=host)


def _inject_auth(
    data: bytes,
    host: str,
    quota_project: str,
    bearer_token: str | None = None,
    bearer_hosts: set[str] | None = None,
) -> bytes:
    """Anade `Authorization` a las peticiones del navegador.

    Prioridad:
      1. `SAPISIDHASH` si el navegador tiene cookie de sesion de Google
         (primera parte / UDS, que es lo que usa la consola de verdad).
      2. `Bearer <token ADC>` para los hosts del backend de la consola si no
         hay cookie de sesion. Asi la consola se puede automatizar SIN login
         de Google: `dialogflow.clients6.google.com` tambien acepta el token
         OAuth estandar (medido: GET de agents en 1,07 s).

    Solo toca el paquete que **abre** una petición HTTP (request-line valida).
    Un body que empieza por "GET " no se confunde con una peticion nueva.
    """
    if not data.startswith((b"GET ", b"POST ", b"PATCH ", b"PUT ", b"DELETE ", b"HEAD ")):
        return data
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        return data
    lines = head.split(b"\r\n")
    # La request-line debe ser "<METHOD> <path> HTTP/1.1"; si no, es un body.
    parts = lines[0].split()
    if len(parts) != 3 or not parts[2].startswith(b"HTTP/"):
        return data
    # El preflight CORS (OPTIONS) NO se autentica: si se le anade un
    # `Authorization` que el navegador no pidio, el navegador rechaza la
    # respuesta del preflight y la XHR nunca se lanza (medido: la consola se
    # queda en blanco al pedir el agente).
    if parts[0].upper() == b"OPTIONS":
        return data
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.decode().strip().lower()] = v.decode().strip()
    if headers.get("authorization"):
        return data  # ya esta autenticada: no se toca
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
    """Proxy MITM local sobre sockets crudos (arranca en un hilo)."""

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

    # ------------------------------------------------------------- interno
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

            # Certificado por host: asi el SSO corporativo (IdP que no es de
            # Google) tambien puede hacer TLS a traves del proxy.
            key, crt = certificate_for_host(self.cert_dir, host)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(str(crt), str(key))
            # ALPN explicito: el navegador debe hablar HTTP/1.1 con el proxy
            # (si no, intenta h2 y el relay HTTP/1.1 no lo entiende).
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
            # NO se traga en silencio: si el TLS con el upstream falla, la
            # peticion del navegador se queda colgada sin explicacion.
            _log(f"ERROR en {host}: {type(exc).__name__}: {exc}")
        finally:
            for s in (conn, upstream):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    def _relay(self, client: ssl.SSLSocket, upstream: ssl.SSLSocket, host: str):
        """Relay bidireccional: **un hilo por direccion**, sockets bloqueantes.

        Por que dos hilos y no `select`:
        con sockets no bloqueantes + `select` hay tres formas de perder bytes
        (medido: la 2ª respuesta HTTP llegaba corrupta, `00000001` en vez de la
        status line):
          * `select` dice "no legible" pero el socket TLS ya tiene datos
            descifrados en su buffer interno (típico con GRO activo);
          * `SSLWantReadError` en `recv` se come un registro TLS a medias;
          * `send()` parcial deja datos en un `pending` que `select` (que solo
            esperaba lectura) no vuelve a despertar.
        Con `recv`/`sendall` **bloqueantes** y un hilo por sentido, el orden y
        la integridad los garantiza el kernel. Ademas `sendall` no devuelve
        hasta que se ha escrito todo.
        """
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
                            _log("   PETICION CRUDA:\n" + data[:1200].decode(errors="replace"))
                    else:
                        if data.startswith(b"HTTP/"):
                            _log(f"<- {data.split(chr(13).encode())[0].decode(errors='replace')[:120]} ({len(data)} B)")
                            _log("   RESPUESTA CRUDA:\n" + data[:1200].decode(errors="replace"))
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
