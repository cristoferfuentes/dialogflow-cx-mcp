# #!/usr/bin/env python3
"""`cx-playbook` CLI: the same logic as MCP, from the terminal.

    .venv/bin/python -m cxp.cli status
    .venv/bin/python -m cxp.cli login                 # opens the browser for login
    .venv/bin/python -m cxp.cli routes   --flow <flow> --page <page>
    .venv/bin/python -m cxp.cli set      --page <page> --route 6 --playbook <playbook>
    .venv/bin/python -m cxp.cli verify   --canal <canal> --es-cliente true"""
from __future__ import annotations

import argparse
import json
import sys

from . import routes as R
from . import verify as V
from .browser import ConsoleBrowser
from .config import load_config
from .cxapi import CxApi

CFG = load_config()


def _api(prefer_console_session: bool = False) -> tuple[CxApi, str, ConsoleBrowser | None]:
    api = CxApi(CFG)
    if prefer_console_session:
        browser = ConsoleBrowser(CFG)
        state = browser.ensure_session(CFG.console_url(CFG.clone_agent))
        if state.logged_in:
            api.use_first_party(browser.auth_headers())
            return api, "primera_parte_consola", browser
        browser.close()
    if api.use_bearer():
        return api, "bearer_gcloud", None
    return api, "sin_autenticacion", None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cxp", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="estado de la sesion y los backends")

    p_login = sub.add_parser("login", help="abre el navegador (con ventana) para iniciar sesion")
    p_login.add_argument("--force", action="store_true")

    p_routes = sub.add_parser("routes", help="lista las rutas de una pagina")
    p_routes.add_argument("--agent", default="clon")
    p_routes.add_argument("--flow", required=True)
    p_routes.add_argument("--page", required=True)
    p_routes.add_argument("--backend", default="public")

    p_set = sub.add_parser("set", help="pone un playbook como destino de una ruta")
    p_set.add_argument("--agent", default="clon")
    p_set.add_argument("--flow", required=True)
    p_set.add_argument("--page", required=True)
    p_set.add_argument("--route", required=True, help="uuid | condicion exacta | indice")
    p_set.add_argument("--playbook", required=True)
    p_set.add_argument("--strategy", default="auto", choices=["auto", "patch", "recreate"])
    p_set.add_argument("--backend", default="auto", choices=["auto", "console", "public"])
    p_set.add_argument("--dry-run", action="store_true")
    p_set.add_argument("--timeout", type=float, default=300.0)

    p_ver = sub.add_parser("verify", help="verifica en runtime que la ruta llega al playbook")
    p_ver.add_argument("--agent", default="clon")
    p_ver.add_argument("--flow", default="")
    p_ver.add_argument("--canal", default="web-privada")
    p_ver.add_argument("--es-cliente", default="true")
    p_ver.add_argument("--timeout", type=float, default=300.0)

    args = ap.parse_args(argv)

    if args.cmd == "status":
        api = CxApi(CFG)
        out = {
            "proyecto": CFG.project,
            "bearer_gcloud": bool(api.use_bearer()),
            "perfil": str(CFG.profile_dir),
            "backend_consola": CFG.console_backend,
        }
        try:
            out["agentes"] = [
                {"id": a["name"].split("/")[-1], "displayName": a.get("displayName")}
                for a in api.list_agents()
            ]
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "login":
        browser = ConsoleBrowser(CFG)
        state = browser.ensure_session(CFG.console_url(CFG.clone_agent), headed_timeout=900)
        print(json.dumps({
            "sesion_valida": state.logged_in,
            "motivo": state.reason,
            "correo": state.user_email,
            "perfil": str(CFG.profile_dir),
        }, ensure_ascii=False, indent=2))
        browser.close()
        return 0 if state.logged_in else 1

    api, mode, browser = _api()
    agent = CFG.original_agent if args.agent == "original" else (
        CFG.clone_agent if args.agent in ("clon", "clone") else args.agent
    )

    if args.cmd == "routes":
        flow_id = api.resolve_flow(agent, args.flow, backend=args.backend)
        page_id = api.resolve_page(agent, flow_id, args.page, backend=args.backend)
        page = api.get_page(agent, flow_id, page_id, backend=args.backend)
        print(json.dumps({
            "agente": agent, "flow": flow_id, "pagina": page_id,
            "displayName": page.get("displayName"),
            "rutas": R.summarize(page.get("transitionRoutes") or []),
            "eventHandlers": page.get("eventHandlers") or [],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "set":
        if args.agent == "original" and not args.dry_run:
            print("ERROR: el agente original es SOLO LECTURA", file=sys.stderr)
            return 2
        be = "public" if args.backend == "public" else "console"
        flow_id = api.resolve_flow(agent, args.flow, backend=be)
        page_id = api.resolve_page(agent, flow_id, args.page, backend=be)
        pb_id = api.resolve_playbook(agent, args.playbook, backend=be)
        strategy = args.strategy
        if strategy == "auto":
            n = len(api.list_playbooks(agent, be))
            strategy = "patch" if n <= 5 else "recreate"
        if strategy == "recreate":
            if args.dry_run:
                print(json.dumps({"dry_run": True, "estrategia": "DELETE+CREATE"}, indent=2))
                return 0
            out = R.recreate_page_with_playbook(
                api, agent, flow_id, page_id, args.route, pb_id, backend=be, timeout=args.timeout
            )
        else:
            out = R.set_route_playbook(
                api, agent, flow_id, page_id, args.route, pb_id,
                backend=be, dry_run=args.dry_run, timeout=args.timeout,
            )
        out["auth"] = mode
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "verify":
        params = {"canal": args.canal, "es_cliente": args.es_cliente.lower() == "true"}
        out = V.verify_route_target(api, agent, args.flow, params, timeout=args.timeout)
        out["auth"] = mode
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("llego_a_playbook") else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
