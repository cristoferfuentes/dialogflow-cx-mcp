"""LIMITS MEASURED of the internal API of Dialogflow CX (2026-11-08).

This module is the **memory of the MCP**: everything that was measured the hard way (the
exact error the API returns, why it returns it and how to work around it) lives
here as DATA, so that the next agent using the MCP does not stumble
on the same thing again or have to re-descubrirlo through HTTP 400s.

It is queried with the `cx_limites_api` tool and is automatically appended to
the errors of the cloning pipeline (`causa` + `workaround`).

The limits of the official discovery doc (`discovery`) do NOT cover this: the public
discovery doc says that `referencedPlaybooks` is read-only and says nothing about
`force`, the `/batch`, playbook loops or latency."""
from __future__ import annotations

from typing import Any

# --- Performance constants (measured on this host, 2026-11-08) ------------
CONSTANTES: dict[str, Any] = {
    "ms_get": 450,
    "ms_post": 560,
    "ms_patch": 470,
    "ms_mano_tls": 660,
    "lecturas_paralelas_optimas": 8,
    "escrituras_paralelas": False,
    "borrado_paralelo_optimo": 2,
    "curva_concurrencia_ops_por_s": {
        # median of 3 repetitions with warm-up (probe_write_concurrency2)
        "POST": {"1": 1.57, "2": 1.01, "3": 1.03, "4": 1.34, "6": 1.14, "8": 1.12},
        "DELETE": {"1": 1.75, "2": 2.10, "3": 1.50, "4": 1.34, "6": 1.21, "8": 1.14},
    },
    "nota": (
        "El tiempo esta en ESPERAR CABECERAS (~450 ms), no en la red: 8 GET "
        "en paralelo tardan ~1 s (121 ms cada uno). El backend SERIALIZA las "
        "escrituras del mismo agente: el RENDIMIENTO se estanca en ~1,5-2 "
        "ops/s sea cual sea la concurrencia (8 POST en paralelo = 899 ms cada "
        "uno, PEOR que 500 ms en serie). La CREACION va en serie; el BORRADO "
        "admite 2 en paralelo (2,10 ops/s contra 1,75: +20%). Con N>=3 el "
        "backend se bloquea y el rendimiento BAJA. El coste escala con el "
        "tamano del cuerpo (+50 KB = +600 ms). El endpoint /batch NO existe "
        "(404)."
    ),
}

# --- API limits (symptom -> cause -> how to work around it) ----------------
LIMITES: list[dict[str, Any]] = [
    {
        "id": "delete_force",
        "titulo": "DELETE con `force=true` (solo en algunos recursos)",
        "sintoma": "HTTP 400 'X are referencing the resource. Please set "
                   "'force' to true or remove these references first'",
        "causa": "El DELETE valida las referencias vivas. `force` esta en el "
                 "descriptor para flows, tools, webhooks, entityTypes, "
                 "generators y transitionRouteGroups, pero NO para playbooks "
                 "ni intents (aun con `force` el playbook se niega).",
        "workaround": "Borrar en orden de dependencias: flows (con force) -> "
                      "playbooks (orden INVERSO de dependencias) -> intents -> "
                      "entityTypes -> tools -> webhooks -> generators. Los "
                      "flows se llevan sus paginas y con ellas las "
                      "referencias a playbooks.",
        "tags": ["borrado", "force", "referencias"],
    },
    {
        "id": "batch_404",
        "titulo": "El endpoint `/batch` no existe en el backend interno",
        "sintoma": "HTTP 404 (pagina de error de Google) al POST a "
                   "/v3beta1/batch",
        "causa": "El descriptor declara `batchPath: batch`, pero el backend "
                 "interno de la consola (clients6) no lo implementa.",
        "workaround": "`cx_batch` lo detecta y **cae a hilos** (las mismas N "
                      "sub-peticiones en paralelo; lo dice en `modo`). Para "
                      "agrupar llamadas a mano: hilos en el servidor MCP "
                      "(lecturas en paralelo) — nunca /batch.",
        "tags": ["batch", "rendimiento", "404"],
    },
    {
        "id": "escrituras_serializadas",
        "titulo": "El backend serializa las escrituras del mismo agente",
        "sintoma": "8 POST en paralelo: 7,2 s (899 ms cada uno) frente a "
                   "~4 s en serie (500 ms cada uno). Curva medida (mediana de "
                   "3 repeticiones, ops/s): POST 1->1,57 | 2->1,01 | 3->1,03 | "
                   "4->1,34 | 6->1,14 | 8->1,12. DELETE 1->1,75 | 2->2,10 | "
                   "3->1,50 | 4->1,34 | 8->1,14.",
        "causa": "Lock de escritura por agente (recompila el agente tras cada "
                 "cambio) + backoff de la espera. El rendimiento se estanca: "
                 "paralelizar mas escrituras NO da mas escrituras por segundo, "
                 "solo mas latencia por escritura.",
        "workaround": "La CREACION va EN SERIE. El BORRADO admite 2 en "
                      "paralelo (los elementos de un mismo grupo son "
                      "independientes): +20% de ops/s. Los playbooks y los "
                      "intents se borran EN SERIE (su orden lo impone el "
                      "servidor). El paralelismo solo ayuda a las LECTURAS "
                      "(8 GET en ~1 s).",
        "tags": ["rendimiento", "escritura", "concurrencia"],
    },
    {
        "id": "ciclos_paginas",
        "titulo": "Las paginas en ciclo necesitan un PATCH extra",
        "sintoma": "Una pagina cuya ruta apunta a una pagina que todavia no "
                   "existe -> HTTP 400 'Target page ... does not exist'",
        "causa": "La API valida el destino en el momento de escribir. En un "
                 "CICLO del grafo de paginas (medido: un ciclo de 11 paginas, "
                 "uno de 3 y una auto-referencia) no se puede crear la pagina "
                 "con su ruta: hay que crearla sin rutas y parchearla "
                 "despues (1 PATCH extra por pagina del ciclo).",
        "workaround": "Ordenar las paginas para minimizar los ciclos rotos: "
                      "busqueda local sobre el coste 'paginas con destino "
                      "sin resolver' (medido: 7 -> 5 PATCH en un agente real "
                      "con 3 ciclos). Y ojo: `END_FLOW` y "
                      "`END_FLOW_WITH_HUMAN_ESCALATION` NO son paginas: son "
                      "validos en cualquier momento (no cuentan como ciclo).",
        "tags": ["creacion", "paginas", "topologia", "ciclos"],
    },
    {
        "id": "rutas_en_el_post",
        "titulo": "Las rutas de un flow pueden ir en su propio POST",
        "sintoma": "Un flow creado sin rutas y luego parcheado cuesta 2 "
                   "escrituras (POST + PATCH) en vez de 1.",
        "causa": "La API valida el destino al escribir, pero si TODAS las rutas "
                 "del flow apuntan a cosas que ya existen, se pueden enviar "
                 "en el POST (medido: `Flujo_Emision` y "
                 "`Flujo_limpiar_autenticacion` -> sus rutas van en el POST).",
        "workaround": "Crear los flows en orden topologico (un flow, DESPUES "
                      "de los flows a los que apuntan sus rutas) y meter en el "
                      "POST las rutas que ya son resolubles. Si el destino es "
                      "una pagina o un playbook (todavia no existen), el flow "
                      "necesita el PATCH de todas formas. El flow por defecto "
                      "siempre necesita PATCH (ya existe: no se puede crear).",
        "tags": ["creacion", "flows", "rutas", "rendimiento"],
    },
    {
        "id": "latencia_por_tamano",
        "titulo": "La latencia de escritura escala con el tamano del cuerpo",
        "sintoma": "flow minimo 559 ms; flow con 50 KB de descripcion 1163 ms",
        "causa": "El backend parsea y valida el JSON completo en cada "
                 "escritura (no es tiempo de red).",
        "workaround": "No hay mucho: el contenido hay que mandarlo. Pero no "
                      "duplicar viajes (nada de GET-verify tras cada "
                      "escritura: verificar al final).",
        "tags": ["rendimiento", "escritura", "payload"],
    },
    {
        "id": "flow_ignora_pages",
        "titulo": "`POST flow` ignora las `pages` del cuerpo",
        "sintoma": "El flow se crea pero sus paginas no (lista vacia)",
        "causa": "El backend no crea paginas anidadas en el POST del flow.",
        "workaround": "Un POST por pagina, en orden topologico.",
        "tags": ["creacion", "paginas"],
    },
    {
        "id": "destinos_validados",
        "titulo": "La API valida TODOS los destinos al escribir",
        "sintoma": "HTTP 400 'Target page ... does not exist in the flow' / "
                   "'Referenced resource X does not exist' / 'Flow X does not "
                   "exist under the agent'",
        "causa": "targetPage, targetFlow, targetPlaybook, intent, "
                 "referencedTools, referencedFlows, referencedPlaybooks y las "
                 "macros ${PLAYBOOK:}/${FLOW:}/${TOOL:} del texto se "
                 "comprueban contra el estado del agente.",
        "workaround": "Crear en orden de dependencias: hojas (entityTypes, "
                      "intents, webhooks, tools) -> flows -> playbooks -> "
                      "generators -> paginas (topologico) -> parches de "
                      "rutas. Para ciclos: crear la pagina sin rutas y "
                      "parchearla despues.",
        "tags": ["creacion", "referencias", "topologia"],
    },
    {
        "id": "end_flow",
        "titulo": "El destino de fin de flow es `.../flows/{flow}/pages/END_FLOW`",
        "sintoma": "Al clonar, la ruta queda apuntando al flow del ORIGINAL",
        "causa": "Es un nombre ANIDADO: hay que remapear el flow DENTRO del "
                 "nombre y la pagina interior. Si solo se remapea el primer "
                 "par, la pagina de dentro se queda con el id viejo.",
        "workaround": "Remapear CADA par `recurso/id` de la ruta (no solo el "
                      "primero). `END_FLOW` no es una pagina: no cuenta como "
                      "destino pendiente.",
        "tags": ["creacion", "remapeo", "paginas"],
    },
    {
        "id": "playbook_loop",
        "titulo": "Un playbook que se referencia a si mismo es IMPOSIBLE",
        "sintoma": "HTTP 400 'Playbook X caused loop in playbook routes.'",
        "causa": "La API rechaza los bucles en el grafo de playbooks. El "
                 "orquestador de un agente real se menciona a si mismo "
                 "(${PLAYBOOK:<su propio id>}) en un texto que "
                 "PROHIBE invocarse a si mismo.",
        "workaround": "Neutralizar SOLO esa macro (token inerte) y quitar la "
                      "entrada de `referencedPlaybooks`. El resto de macros se "
                      "dejan intactas. Es una desviacion inevitable por API.",
        "tags": ["creacion", "playbooks", "bucles"],
    },
    {
        "id": "macros_validadas",
        "titulo": "Las macros del texto se validan al crear/actualizar",
        "sintoma": "HTTP 400 al crear un playbook con ${PLAYBOOK:x} si x no "
                   "existe todavia (o si x es el propio playbook)",
        "causa": "El backend resuelve las macros del instruction/codeBlock "
                 "contra el agente.",
        "workaround": "Crear los playbooks en orden topologico (sus "
                      "referencias y las de otros playbooks, primero) y "
                      "neutralizar unicamente la autorreferencia.",
        "tags": ["creacion", "playbooks", "macros"],
    },
    {
        "id": "campos_solo_lectura",
        "titulo": "`referencedFlows`, `referencedPlaybooks` e `inlineActions` "
                  "son de SOLO LECTURA (se derivan)",
        "sintoma": "El POST los ignora (vuelven null) y el PATCH falla si se "
                   "mandan: 'Agent default language code cannot be updated' / "
                   "'Flow X does not exist'",
        "causa": "El backend los calcula a partir de las macros del texto y "
                 "de las referencias de flows/paginas.",
        "workaround": "No mandarlos al escribir: se dejan que el backend los "
                      "derive. `defaultLanguageCode` NO se puede actualizar.",
        "tags": ["creacion", "campos", "playbooks"],
    },
    {
        "id": "tool_builtin",
        "titulo": "Las tools builtin no se pueden crear ni borrar",
        "sintoma": "POST: 'Builtin tools are provided by Dialogflow and "
                   "should not be created manually'. DELETE: 404 'Tool "
                   "df-code-interpreter-tool does not exist'",
        "causa": "Son del Dialogflow (id `df-*`) y ya existen en cualquier "
                 "agente.",
        "workaround": "Saltarlas: no crear, no borrar, y remapear su nombre a "
                      "si mismo.",
        "tags": ["tools", "builtin"],
    },
    {
        "id": "tool_openapi_vacio",
        "titulo": "Un tool con el OpenAPI vacio no se puede recrear",
        "sintoma": "HTTP 400 'OpenApiSchemaParser$BadParseException: Found "
                   "errors in the Open API specification [Null or empty "
                   "definition]'",
        "causa": "En el original hay tools duplicados con `openApiSpec` sin "
                 "`textSchema` (copias con fecha).",
        "workaround": "Crearlos SIN `openApiSpec`/`dataStoreSpec`: el elemento "
                      "existe y los playbooks que lo referencian ya no "
                      "fallan. Queda como desviacion documentada.",
        "tags": ["tools", "fallback"],
    },
    {
        "id": "tool_datastore_borrado",
        "titulo": "Un tool cuyo data store ya no existe no se puede recrear",
        "sintoma": "HTTP 400 'Failed to connect datastores ... Data store(s) "
                   "... do not exist'",
        "causa": "El data store al que apunta el tool fue borrado: el "
                 "original lo referencia y ya no valida.",
        "workaround": "Fallback: crear el tool SIN `dataStoreSpec` "
                      "(mismo nombre y descripcion) y anotarlo como "
                      "desviacion.",
        "tags": ["tools", "fallback", "datastore"],
    },
    {
        "id": "start_flow_protegido",
        "titulo": "El flow por defecto y los intents por defecto estan "
                  "protegidos",
        "sintoma": "DELETE flow: 'Cannot delete the start flow 00000000-...'. "
                   "PATCH de eventHandlers: 'You cannot delete event handler "
                   "for sys.no-match-default'",
        "causa": "El `Default Start Flow` (00000000-0000-0000-0000-000000000000) "
                 "y los intents 00000000-...-0000/0001 los crea el backend en "
                 "todo agente.",
        "workaround": "No borrarlos: se PATCHEAN para que su contenido "
                      "coincida con el original (y el flow por defecto solo "
                      "con `transitionRoutes`, sin tocarle los handlers).",
        "tags": ["borrado", "flows", "intents"],
    },
    {
        "id": "verificar_al_final",
        "titulo": "Verificar tras cada escritura duplica el trafico",
        "sintoma": "Un clonado con GET-verify por escritura tarda ~2x",
        "causa": "Cada escritura con su GET de comprobacion son 2 viajes de "
                 "~0,5 s.",
        "workaround": "Escribir sin verificar y comparar UNA vez al final "
                      "(conteos + firma de routing por pagina).",
        "tags": ["rendimiento", "verificacion"],
    },
    {
        "id": "guard_metodo_propio",
        "titulo": "El id del agente llega con el sufijo `:metodo` en los "
                  "metodos propios",
        "sintoma": "'el agente X:export no tiene permiso' aunque X este en la "
                   "lista blanca",
        "causa": "`agent_from_path` parseaba `.../agents/{id}:export` y "
                 "comparaba `{id}:export` con la lista.",
        "workaround": "Ya corregido en `cxp/guard.py`: se corta el sufijo "
                      "`:metodo` antes de comparar.",
        "tags": ["guardas", "permisos"],
    },
    # ------------------------------------------------------------------
    # The limits of WRITE FIELDS OUTSIDE OF SCHEMA (measured with the
    # probe `probes/probe_console_only_writable3.py`).
    # ------------------------------------------------------------------
    {
        "id": "campos_fuera_de_esquema",
        "titulo": "La API publica descarta en silencio los campos que no "
                  "declara; el backend de la consola conserva algunos",
        "sintoma": "Un PATCH de un campo que la API publica no declara "
                   "devuelve HTTP 200 y el campo NO esta: se pierde sin "
                   "error. Por la API publica tampoco se puede LEER.",
        "causa": "El proto publico de v3beta1 no tiene `TransitionRoute."
                 "target_playbook`, `FormParameter.description`, "
                 "`EntityTypeEntity.dtmf_pattern` ni `Agent.web_client_"
                 "settings`. El proto del backend de la consola es mas rico.",
        "workaround": "Escribir contra el backend interno (este MCP lo hace) "
                      "y VERIFICAR siempre releyendo: `cx_set_console_field` "
                      "compara antes/despues y dice que se guardo y que se "
                      "perdio. `cx_gaps` lista los huecos, medidos en vivo.",
        "tags": ["esquema", "campos", "agujeros"],
    },
    {
        "id": "patch_etag_obsoleto",
        "titulo": "Un PATCH con el `etag` viejo devuelve 409",
        "sintoma": "HTTP 409 FAILED_PRECONDITION 'Others may have made recent "
                   "changes to the X. Please refresh your CX console to load "
                   "the updated resource.'",
        "causa": "El GET del backend devuelve un `etag` y si se reenvia en el "
                 "PATCH, el servidor lo compara con el suyo y lo rechaza.",
        "workaround": "Quitar el `etag` del cuerpo antes de PATCHear (la "
                      "consola tampoco lo manda). Lo hace `strip_derived()` "
                      "en `cxp/gaps.py`.",
        "tags": ["patch", "etag", "concurrencia"],
    },
    {
        "id": "ruta_sin_intent_ni_condition",
        "titulo": "Una transition route necesita `intent` o `condition`",
        "sintoma": "HTTP 400 'At least one of intent or condition in "
                   "transition route should be specified.'",
        "causa": "El servidor valida la ruta al escribirla, aunque el campo "
                 "este en el descriptor.",
        "workaround": "Poner siempre `condition` (o `intent`) en la ruta. Si "
                      "se crea una ruta nueva con `cx_set_console_field`, "
                      "incluirla en el objeto.",
        "tags": ["transiciones", "validacion"],
    },
    {
        "id": "ruta_sin_destino",
        "titulo": "Una transition route de un GRUPO DE RUTAS necesita destino",
        "sintoma": "HTTP 400 'At least one of trigger fulfillment, target "
                   "page, target flow, or target playbook in transition route "
                   "should be specified.'",
        "causa": "En `transitionRouteGroups` la ruta no puede quedar "
                 "pendiente: a diferencia de una pagina, no hay un "
                 "post-proceso que la complete.",
        "workaround": "Darle destino al crear el grupo (por ejemplo "
                      "`targetPlaybook`, que la API publica no puede "
                      "expresar: ver `cx_set_group_route_playbook`).",
        "tags": ["transiciones", "grupos de rutas", "validacion"],
    },
    {
        "id": "subcampos_desconocidos",
        "titulo": "Al escribir un mensaje, el servidor vacia los subcampos "
                  "que no conoce",
        "sintoma": "Escribir `advancedSettings` deja `enabled` sin aparecer y "
                   "`playbackInterruptionSettings` en `{}` (medido).",
        "causa": "Esos subcampos los lleva la consola por su cuenta: el "
                 "backend los deriva y descarta lo que se le mande.",
        "workaround": "No intentar escribirlos: son de solo lectura "
                      "(`estado: derivado` en el catalogo de `cx_gaps`).",
        "tags": ["campos", "derivados"],
    },
    {
        "id": "action_schemas_derivadas",
        "titulo": "Los `actionSchemas` de un tool los deriva el servidor",
        "sintoma": "Escribir `Tool.actionSchemas` devuelve HTTP 200 y no "
                   "cambia nada: siguen los que habia.",
        "causa": "El servidor los genera a partir del `openApiSpec` o del "
                 "`dataStoreSpec` del tool.",
        "workaround": "Solo de lectura. Para cambiar el contrato de un tool "
                      "hay que cambiar su spec, no sus actionSchemas.",
        "tags": ["tools", "derivados"],
    },
    {
        "id": "tipos_no_declarados_se_validan",
        "titulo": "Los campos que la API publica no declara tambien se "
                  "validan (y con tipos que no se ven)",
        "sintoma": "HTTP 400 \"Invalid value at 'agent.web_client_settings' "
                   "(type ...Agent.WebClientSettings), \\\"8\\\"\" al mandar "
                   "un numero donde va un texto.",
        "causa": "`boxBorderRadius` es un STRING en el proto del backend, "
                 "pero no aparece en el descriptor publico, asi que no se "
                 "puede saber de antemano.",
        "workaround": "Copiar el valor que ya tiene el recurso (el GET lo "
                      "devuelve) y cambiar solo el tipo.",
        "tags": ["tipos", "campos", "validacion"],
    },
    {
        "id": "cookies_jar_incompleto",
        "titulo": "Las 5 cookies NO bastan: el backend pide el jar completo",
        "sintoma": "HTTP 401 'Request had invalid authentication credentials. "
                   "Expected OAuth 2 access token, login cookie or other valid "
                   "authentication credential.'",
        "causa": "Las cookies de sesion de Google (`__Secure-*PSIDTS`, "
                 "`__Host-GAPS`, `SIDCC`, `OSID`...) tambien se revalidan. "
                 "MEDIDO (2026-11-13) con el mismo binario y el mismo "
                 "`:env`: con solo `SAPISID, SID, HSID, SSID, APISID` -> 401; "
                 "con las 23 cookies de `.google.com` -> 200.",
        "workaround": "`CXP_COOKIE` debe llevar el jar COMPLETO de `.google.com` "
                      "(copiado del navegador). Las 5 siguen siendo el minimo "
                      "que se comprueba (`missing_cookies()`), pero no el "
                      "conjunto que se manda. `probes/renueva_cookies.py` las "
                      "renueva desde el Chrome real.",
        "tags": ["auth", "cookies", "401"],
    },
    {
        "id": "page_size_maximo_100",
        "titulo": "El backend interno acota `pageSize` a 100 (el descriptor no)",
        "sintoma": "HTTP 400 'Page size should be within [0, 100].' al pedir "
                   "`pageSize=200`.",
        "causa": "El descriptor publico declara `pageSize` sin maximo, pero el "
                 "backend de la consola aplica un tope. MEDIDO: `agents/{a}/"
                 "environments` con `pageSize=200` -> 400; con 100, 50, 20 o 5 "
                 "-> 200. Ojo: `agents/{a}/flows` con 200 SI funciona: el tope "
                 "es por endpoint, no global.",
        "workaround": "Pedir 100 (o menos). `cxp.gaps._listar()` reintenta "
                      "sola con 50, 20 y 5 si el servidor lo rechaza.",
        "tags": ["listado", "pageSize", "400"],
    },
    {
        "id": "detect_intent_exige_language_code",
        "titulo": "`detectIntent` exige `queryInput.languageCode`",
        "sintoma": "HTTP 400 \"Field 'query_input.language_code' is missing.\"",
        "causa": "El idioma va en `queryInput.languageCode`, NO dentro de "
                 "`queryInput.text.languageCode` (esa forma se descarta y el "
                 "servidor responde 400). MEDIDO (2026-11-13) con las tres "
                 "formas: `queryInput.languageCode` -> 200; "
                 "`queryInput.text.languageCode` -> 400; `languageCode` en la "
                 "raiz -> 400.",
        "workaround": "Cuerpo: `{\"queryInput\": {\"text\": {\"text\": ...}, "
                      "\"languageCode\": \"es\"}}`. La ruta es "
                      "`agents/{a}/sessions/{sessionId}:detectIntent` "
                      "(NO `sessions:detectIntent`: esa da 404).",
        "tags": ["detectIntent", "sesion", "400"],
    },
    {
        "id": "entorno_exige_todos_los_flows",
        "titulo": "Crear un entorno exige versionConfigs con TODOS los flows",
        "sintoma": "HTTP 400 \"Flow 'X' is referenced in version 'Y' of flow "
                   "'Z', but not included in the environment.\" y, en la "
                   "siguiente vuelta, \"Playbook 'P' is referenced in version "
                   "'Y' of flow 'Z', but not included in the environment.\"",
        "causa": "La version publicada arrastra referencias a otros flows **y "
                 "a playbooks**, y el entorno tiene que incluirlas todas en "
                 "`versionConfigs`. El proto publico no declara ningun campo "
                 "para meter un playbook en el `versionConfig`: el hueco se "
                 "nota justo al crear el entorno.",
        "workaround": "Publicar una version de CADA flow del agente y meterlas "
                      "todas en `versionConfigs`. Si ademas falla por un "
                      "playbook, hay que publicar las versiones de playbook "
                      "tambien.",
        "tags": ["entornos", "versiones", "400"],
    },
    {
        "id": "test_cases_sin_delete",
        "titulo": "`testCases` no tiene DELETE: se borran en lote",
        "sintoma": "DELETE a `agents/{a}/testCases/{id}` -> **404** (no es un "
                   "problema de permisos: el metodo no existe).",
        "causa": "El descriptor solo declara `batchDelete` "
                 "(`POST agents/{a}/testCases:batchDelete`) y `patch`. El "
                 "borrado de un caso suelto no existe en la API.",
        "workaround": "`POST agents/{a}/testCases:batchDelete` con "
                      "`{\"names\": [\"projects/.../testCases/{id}\"]}` (medido: "
                      "borra los 6 de golpe y la lista queda vacia).",
        "tags": ["testCases", "borrado", "404"],
    },
    {
        "id": "endpoints_fuera_del_descriptor",
        "titulo": "La consola llama a endpoints que NO estan en el descriptor",
        "sintoma": "La UI muestra datos (tipos de entidad del sistema, usuarios "
                   "con acceso, modelos disponibles, proyectos recientes) que "
                   "la API publica no expone: no hay metodo que pedir.",
        "causa": "El discovery doc publico y el del backend interno son "
                 "EL MISMO DOCUMENTO (medido: 669 652 bytes cada uno, mismo md5 "
                 "canonico 29adf74f..., los mismos 162 "
                 "metodos en v3beta1): lo que falta no es un metodo, son "
                 "endpoints que la consola usa y que no estan en NINGUN "
                 "descriptor (`systemEntityTypes:list`, `currentUsers`, "
                 "`:listAvailableModels`, `:getSupportedLanguages`, "
                 "`users/me/userSettings/recentProjects`, `agent/visibility`, "
                 "`conversationProfiles`, `locationSettings`).",
        "workaround": "`cx_api` con `version=` y rutas absolutas los alcanza "
                      "todos: ver el catalogo en `cxp/console_endpoints.py` "
                      "(`HUECOS_METODO`) y la herramienta `cx_endpoints_consola`.",
        "tags": ["metodos", "descriptor", "consola"],
    },
]


def buscar(filtro: str = "") -> list[dict[str, Any]]:
    """Limits filtered by text (id, title, symptom, cause, workaround)."""
    if not filtro:
        return LIMITES
    f = filtro.lower()
    out = []
    for lim in LIMITES:
        texto = " ".join(str(lim.get(k, "")) for k in
                         ("id", "titulo", "sintoma", "causa", "workaround",
                          "tags")).lower()
        if f in texto:
            out.append(lim)
    return out


def causa_de(mensaje: str) -> dict[str, Any] | None:
    """Deduces the known limit that explains an error from the API.

    Used to automatically annotate pipeline errors: the agent that
    reads the report sees, next to the HTTP 400, the cause and the workaround."""
    m = (mensaje or "").lower()
    if not m:
        return None
    firmas = [
        ("caused loop in playbook routes", "playbook_loop"),
        ("null or empty definition", "tool_openapi_vacio"),
        ("failed to connect datastores", "tool_datastore_borrado"),
        ("builtin tools are provided by dialogflow", "tool_builtin"),
        ("cannot delete the start flow", "start_flow_protegido"),
        ("you cannot delete event handler", "start_flow_protegido"),
        ("default language code cannot be updated", "campos_solo_lectura"),
        ("please set 'force' to true", "delete_force"),
        ("are referencing the", "delete_force"),
        ("does not exist in the flow", "destinos_validados"),
        ("referenced resource", "destinos_validados"),
        ("does not exist under the agent", "destinos_validados"),
    ]
    for firma, id_lim in firmas:
        if firma in m:
            for lim in LIMITES:
                if lim["id"] == id_lim:
                    return lim
    return None
