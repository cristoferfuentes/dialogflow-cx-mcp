# Cómo contribuir

Gracias por querer ayudar. Esto es un MCP pequeño: el valor está en que **todo
lo que dice está medido**, no supuesto.

## Regla de oro: lo que afirmes, mídelo

Cada afirmación sobre el comportamiento de la API (un error concreto, un límite,
un campo que "no se puede escribir") debe venir de **una ejecución real**, no de
la documentación ni de la memoria. Si no lo has medido, no lo escribas.

## Cómo medir

Las sondas de `probes/` son elinstrumento. Las que produjeron las mediciones
publicadas son:

| sonda | qué mide |
|---|---|
| `probes/probe_schema_gaps.py` | qué campos devuelve el backend que el esquema público no declara |
| `probes/probe_console_only_writable.py` | qué campos fuera de esquema **conserva** el servidor al escribir |
| `probes/probe_console_only_writable3.py` | la matriz final (usa el código de producción: `cxp.console_fields.set_field`) |
| `probes/test_herramientas_nuevas.py` | las herramientas nuevas, de punta a punta |

Los ids de agente **no se cablean**: salen del entorno.

```bash
export CXP_PROBE_AGENTS="por_defecto=<uuid>"
export CXP_PROBE_TARGETS="por_defecto=<agente>,<flow>,<pagina>"
.venv/bin/python probes/probe_schema_gaps.py
```

**Regla dura al medir:** escribe solo en un agente de `CXP_AGENT_READWRITE`
(una copia tuya). Nunca en un agente de referencia ni en el de producción: se
rompería la paridad con la consola. Crea recursos temporales y bórralos.

## Antes de abrir un PR

```bash
pytest                  # tests unitarios (sin red)
python smoke_test.py    # prueba de punta a punta (necesita credenciales)
ruff check cxp server.py tests
```

* Los **tests unitarios** no pueden tocar la red. Si tu cambio necesita
  verificar algo del backend, va en `probes/`, no en `tests/`.
* El **smoke test** debe seguir siendo genérico: no puede tener ids de agente,
  flows ni playbooks cableados, ni conteos fijos.
* Si tocas una capa, el smoke test tiene que **llamar a cada herramienta** que
  use esa capa.

## Estilo

* Código y comentarios en **español** (es lo que usa el resto del repo).
* Los docstrings de las herramientas MCP son su **API**: explica qué hace, qué
  parámetros tiene y qué errores puede dar.
* Sin dependencias nuevas sin motivo: el MCP funciona solo con `mcp`.

## Al añadir un límite o un hueco

1. Mídelo (sonda) y anota el **síntoma exacto** que devuelve el servidor.
2. Añádelo a `cxp/limits.py` (límite) o a `cxp/gaps.py` (hueco), con su causa y
   su rodeo.
3. Si es un hueco, di **si se puede escribir** (estado `escribible` /
   `derivado` / `solo_lectura`): es la información que más valor da.

## Seguridad

* **Nunca** subas un `.env`, cookies, un `storage_state` ni un id de agente real
  a un PR.
* Las cookies de sesión son credenciales: trátalas como una contraseña.
