"""Write GENERIC of a field that the public API does not declare.

The idea: there is no per-resource method and no per-gap method. There is ONE
operation:

    GET  resource (by its `name`)
    -> the value is applied on the requested route (`a.b[0].c`)
    -> the resource is PATCHed with the minimal mask (the top-level key)
    -> it is re-read and COMPARES

The final comparison is what gives the guarantee: the server can **drop**
a field silently (that is what the public API does; the console
backend also does it with some fields). That is why the response says what actually
changed and what was lost."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .gaps import apply_path, read_path, strip_derived, top_level_keys


def _recortar(valor: Any, limite: int = 300) -> Any:
    import json
    try:
        texto = json.dumps(valor, ensure_ascii=False)
    except (TypeError, ValueError):
        texto = str(valor)
    return texto[:limite] + "..." if len(texto) > limite else valor


def diff_json(antes: Any, despues: Any, prefijo: str = "") -> list[dict[str, Any]]:
    """Differences between two structures, in `{ruta, antes, despues}` form."""
    cambios: list[dict[str, Any]] = []
    if isinstance(antes, dict) and isinstance(despues, dict):
        for clave in sorted(set(antes) | set(despues)):
            ruta = f"{prefijo}.{clave}" if prefijo else clave
            if clave not in antes:
                cambios.append({"ruta": ruta, "antes": None, "despues": _recortar(despues[clave])})
            elif clave not in despues:
                cambios.append({"ruta": ruta, "antes": _recortar(antes[clave]), "despues": None})
            else:
                cambios.extend(diff_json(antes[clave], despues[clave], ruta))
        return cambios
    if isinstance(antes, list) and isinstance(despues, list):
        for i in range(max(len(antes), len(despues))):
            ruta = f"{prefijo}[{i}]"
            if i >= len(antes):
                cambios.append({"ruta": ruta, "antes": None, "despues": _recortar(despues[i])})
            elif i >= len(despues):
                cambios.append({"ruta": ruta, "antes": _recortar(antes[i]), "despues": None})
            else:
                cambios.extend(diff_json(antes[i], despues[i], ruta))
        return cambios
    if antes != despues:
        cambios.append({"ruta": prefijo or "$", "antes": _recortar(antes),
                        "despues": _recortar(despues)})
    return cambios


@dataclass
class Resultado:
    recurso: str
    campo: str
    valor: Any
    escrito: bool = False
    verificado: bool = False
    mascara: list[str] = field(default_factory=list)
    antes: Any = None
    despues: Any = None
    cambios: list[dict[str, Any]] = field(default_factory=list)
    perdidas: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "recurso": self.recurso,
            "campo": self.campo,
            "valor": _recortar(self.valor),
            "mascara_update": self.mascara,
            "escrito": self.escrito,
            "verificado": self.verificado,
        }
        if self.antes is not None:
            d["valor_antes"] = _recortar(self.antes)
        if self.despues is not None:
            d["valor_despues"] = _recortar(self.despues)
        if self.cambios:
            d["cambios"] = self.cambios[:40]
        if self.perdidas:
            d["descartado_por_el_servidor"] = self.perdidas
        if self.error:
            d["error"] = self.error
        return d


def set_field(api, resource_name: str, path: str, value: Any, *,
              dry_run: bool = True, project: str = "",
              language_code: str | None = None,
              campos_extra: tuple[str, ...] = ()) -> Resultado:
    """Write `value` to `path` of resource `resource_name`.

    `resource_name` is the complete `name` returned by the API
    (`projects/p/locations/l/agents/a/flows/f/pages/p`), so it works with
    ANY agent resource without writing a method per type.

    `path` is a route with indices: `transitionRoutes[0].targetPlaybook`."""
    res = Resultado(recurso=resource_name, campo=path, valor=value)
    try:
        antes = api.request("GET", api._url("/" + resource_name))
    except Exception as exc:
        res.error = f"no se pudo leer el recurso: {exc}"
        return res

    valor_antes = read_path(antes, path)
    res.antes = valor_antes

    if dry_run:
        # Shows what WOULD PASS, without writing.
        simulado = copy.deepcopy(antes)
        try:
            apply_path(simulado, path, value)
        except ValueError as exc:
            res.error = str(exc)
            return res
        res.despues = read_path(simulado, path)
        res.mascara = top_level_keys(path)
        res.cambios = diff_json(antes, simulado)
        return res

    cuerpo = strip_derived(copy.deepcopy(antes), extra=campos_extra)
    try:
        apply_path(cuerpo, path, value)
    except ValueError as exc:
        res.error = str(exc)
        return res

    mascara = top_level_keys(path)
    res.mascara = mascara
    try:
        api.patch_resource(resource_name, cuerpo, update_mask=",".join(mascara),
                           language_code=language_code, project=project)
        res.escrito = True
    except Exception as exc:
        res.error = f"fallo el PATCH: {exc}"
        return res

    try:
        despues = api.request("GET", api._url("/" + resource_name))
    except Exception as exc:
        res.error = f"se escribio pero no se pudo releer: {exc}"
        return res

    res.despues = read_path(despues, path)
    res.verificado = res.despues == value
    res.cambios = diff_json(antes, despues)
    # What was asked to be written and the server did NOT save.
    perdidas = []
    if not res.verificado:
        perdidas.append(path)
    for cambio in res.cambios:
        if cambio["ruta"].startswith(path.split("[")[0]) and cambio["despues"] is None:
            perdidas.append(cambio["ruta"])
    res.perdidas = sorted(set(perdidas))
    return res
