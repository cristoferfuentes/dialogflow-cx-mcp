# MCP `cx-playbook` — espejo de la API **v3beta1** de Dialogflow CX **+ los huecos que la API pública no puede expresar**

Un servidor [MCP](https://modelcontextprotocol.io) para operar Dialogflow CX.

Hace **dos cosas**:

1. **Espejo de la API pública v3beta1** (los 162 métodos / 32 recursos del
   *discovery doc* oficial), con herramientas tipadas para el uso diario y un
   passthrough genérico 1:1.
2. **Tapa los agujeros de la API pública**: hay cosas que la consola de
   Dialogflow CX puede hacer y la API pública **no puede ni leer ni escribir**
   (por ejemplo, poner un *playbook* como destino de una transición). Este MCP
   las hace, y además **te dice cuáles son** (`cx_gaps`).

**No tiene proyecto ni agente cableados**: todo se configura en un `.env`.
**No necesita navegador**: la única credencial es una cabecera `Cookie`.

> ⚠️ **No es un producto de Google.** Habla con el backend interno de la
> consola (`dialogflow.clients6.google.com`) usando la autenticación de
> primera parte de tu propia cuenta. Revisa el [estado legal](#aviso-legal).

---

## Índice

- [Por qué el backend interno](#por-qué-el-backend-interno)
- [Los huecos de la API pública](#los-huecos-de-la-api-pública)
- [Instalación](#instalación)
- [Cómo obtener las credenciales](#cómo-obtener-las-credenciales)
- [Referencia del `.env`](#referencia-del-env)
- [Registro en un cliente MCP](#registro-en-un-cliente-mcp)
- [Herramientas](#herramientas)
- [Instrucciones para el agente que usa este MCP](#instrucciones-para-el-agente-que-usa-este-mcp)
- [Límites medidos de la API](#límites-medidos-de-la-api)
- [Detalles técnicos (medidos)](#detalles-técnicos-medidos)
- [Desarrollo](#desarrollo)
- [Aviso legal](#aviso-legal)

---

## Por qué el backend interno

La API pública de Dialogflow CX (`dialogflow.googleapis.com/v3beta1`) y el
backend interno que usa la consola (`dialogflow.clients6.google.com/v3beta1`)
sirven **el mismo *discovery doc***, pero **no el mismo proto**:

* El **proto público** de `TransitionRoute` es un `oneof` con `target_page` y
  `target_flow`. **`target_playbook` no existe.** Un `PATCH` que lo envíe se
  **descarta en silencio**: la ruta queda sin destino y el agente cae en
  `NO_MATCH`.
* El **proto del backend de la consola** es más rico: conoce campos que la API
  pública **ni siquiera declara** en el descriptor.

Por eso comparar los dos descriptores no encuentra nada (son idénticos): lo que
difiere es **qué campos acepta cada proto**. Y por eso este MCP:

* habla con el backend de la consola (autenticación de primera parte: tus
  cookies de Google + firma `SAPISIDHASH`), y
* trae una capa que **compara el JSON real con el esquema público** para
  detectar los campos que la API pública no puede expresar.

Detalle fino: **usa `v3beta1`, nunca `v3`**. En `v3` no existen campos como
`llmModelSettings.parameters` y se descartan en silencio.

---

## Los huecos de la API pública

Un **hueco** es un campo que el backend de la consola expone y que la API
pública **no declara**: por la API pública no se puede ni leer ni escribir.

El MCP los detecta **en vivo** (`cx_gaps`), comparando cada recurso del agente
con el esquema público del *discovery doc* oficial. No es una lista escrita a
mano: si Google añade un campo nuevo a la consola, el MCP lo ve sin tocar el
código.

### Lo que se puede ESCRIBIR (medido: escrito y releído)

| Recurso | Campo | Herramienta |
|---|---|---|
| `Page`, `Flow`, `TransitionRouteGroup` | `transitionRoutes[].targetPlaybook` | `cx_set_route_playbook`, `cx_set_flow_route_playbook`, `cx_set_group_route_playbook` |
| `Page` | `form.parameters[].description` | `cx_set_console_field` |
| `EntityType` | `entities[].dtmfPattern` | `cx_set_console_field` |
| `Agent` | `webClientSettings` (p. ej. `boxBorderRadius`) | `cx_set_console_field` |

### Lo que solo se puede LEER (el servidor lo deriva)

| Recurso | Campo | Por qué |
|---|---|---|
| `Tool` | `actionSchemas`, `sharedSchemas` | el servidor los deriva del `openApiSpec`/`dataStoreSpec`; escribirlos no cambia nada |
| `Agent`/`Page`/`Flow` | `advancedSettings.enabled`, `.playbackInterruptionSettings`, `.speechAdaptationSettings` | al escribir el mensaje, el servidor vacía los subcampos desconocidos |
| `Agent` | `speakerIdSettings` | el servidor lo devuelve como `{}` y descarta lo que se le escriba |
| `Flow` | `modelIdentifier`, `nluSettingsList`, `nluSettings.customConfigKey` | los regenera el servidor |
| `Intent` | `trainingPhrases[].parts[].auto` | marca de frase autogenerada |
| `Tool` | `dataStoreSpec.models`, `.groundingSettings` | modelo de resumido y *grounding* del tool de data store |
| `Agent` | `projectNumber` | el número de proyecto que ve la consola |
| `Playbook` | `speechAdaptationSettings` | ajustes de adaptación de voz |

### El caso grande: `targetPlaybook`

La API pública v3beta1 tiene **tres** vías para llegar a un playbook, y
ninguna es equivalente a "una transición → un playbook":

| vía pública | ¿existe? | ¿equivale a "ruta → playbook"? |
|---|---|---|
| `EventHandler.targetPlaybook` | ✅ | ❌ el `EventHandler` **no tiene `condition`**: dispara por evento, no por parámetros de sesión |
| `Agent.startPlaybook` | ✅ | ❌ es un punto de entrada del **agente**, no de un flow ni de una ruta |
| `Playbook.referencedPlaybooks` / `inlineActions` | ✅ pero `readOnly: true` | ❌ no se pueden escribir |

Y lo que **no existe**: `TransitionRoute.targetPlaybook`. Como la
`TransitionRoute` es **el mismo mensaje** en los tres sitios donde vive (la
página, el router del flow y el grupo de rutas), el hueco se repite en los
tres, y el MCP lo cubre en los tres:

| nivel | cubre | qué habilita |
|---|---|---|
| **página** | `cx_set_route_playbook` | "dada una condición en ESTA página → playbook X" |
| **flow** | `cx_set_flow_route_playbook` | "**al entrar** a este flow → playbook X" (imposible por API pública: a un flow solo se entra con `targetFlow` o `Agent.startFlow`) |
| **grupo de rutas** | `cx_set_group_route_playbook` | una ruta compartida por varias páginas → playbook X |

Diferencias de comportamiento (medidas):

* **Radio**: las rutas de nivel flow **se heredan a todas las páginas del flow**
  (y las de página tienen prioridad sobre ellas). Solo las rutas de flow **con
  intent** se heredan; las solo-condición aplican en la entrada del flow.
* **Runtime**: las ejecuta en los dos niveles (medido con un playbook de marca
  propia: `matchType=PLAYBOOK`, `currentPlaybooks=[el playbook nuevo]` y la
  respuesta fue su texto exacto).
* **Escrituras**: 1 (flow, con intent) frente a N (una por página).

⚠️ **`verificado: true` es una comprobación de API, no de runtime.** El runtime
de CX va **por detrás** de la API: después de revertir una ruta y borrar un
playbook, el GET mostraba el estado limpio pero el runtime siguió ejecutando
la ruta vieja (`HTTP 404 "Action with id <playbook borrado> not found"`) hasta
sincronizarse. Consecuencia práctica: **no borres un playbook al que apunte
una ruta sin quitar antes la ruta.**

---

## Instalación

```bash
git clone https://github.com/<TU-USUARIO>/mcp-cx-playbook.git
cd mcp-cx-playbook
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # y rellenarlo (ver abajo)
```

O, sin instalar el paquete:

```bash
.venv/bin/pip install "mcp>=1.2"
```

---

## Cómo obtener las credenciales

El MCP autentica contra el backend interno de la consola con **primera parte**:
las cookies de sesión de tu navegador de Google. No hace falta token de servicio.

### 1. Abre la consola con tu sesión iniciada

Entra en la consola de Conversational Agents con la cuenta que tiene acceso al
proyecto (la misma que usas en la UI):

```
https://conversational-agents.cloud.google.com/projects/<PROYECTO>/locations/global/agents/<AGENTE>
```

### 2. Abre DevTools y busca una petición al backend

`F12` → pestaña **Network** → marca **Preserve log** → **recarga** la página.
Filtra por `clients6.google.com` (o `dialogflow`). Verás peticiones como:

```
GET https://dialogflow.clients6.google.com/v3beta1/projects/.../agents/.../flows?key=AIzaSy...
```

### 3. Copia la cabecera `Cookie`

Clic en esa petición → **Request Headers** → copia **todo** el valor de la
cabecera `Cookie` (no solo `SAPISID`). Tendrá esta forma:

```
SAPISID=XXXX; SID=YYYY; HSID=ZZZZ; SSID=WWWW; APISID=VVVV; ...
```

### 4. Pégalo en el `.env`

```bash
CXP_COOKIE="SAPISID=XXXX; SID=YYYY; HSID=ZZZZ; SSID=WWWW; APISID=VVVV; ..."
```

Las **5 cookies que hacen falta** son `SAPISID`, `SID`, `HSID`, `SSID` y `APISID`
(medido: con 4 de 5 el backend responde **401**; con cookies de otros dominios
también). Si te da 401, copia la cabecera `Cookie` **entera** otra vez: puede que
el orden o alguna cookie extra importen.

### 5. La API key

`CXP_API_KEY` es la clave pública que usa la consola de Google. La puedes leer en
la propia URL de la petición del paso 2: es el parámetro `key=`. El valor por
defecto del ejemplo es el de la consola de Dialogflow.

### Alternativa: `storage_state` de Playwright

Si ya tienes un `storage_state` (un JSON con `cookies`), ponlo en
`CXP_SESSION_FILE` y el MCP extrae las 5 cookies solo. La cookie debe ser de
dominio `.google.com`.

---

## Referencia del `.env`

| variable | obligatorio | para qué |
|---|---|---|
| `CXP_PROJECT` | sí | id del proyecto de Google Cloud (por defecto en las llamadas) |
| `CXP_LOCATION` | no (`global`) | location de Dialogflow CX |
| `CXP_CONSOLE_ORIGIN` | no | origen de la consola; entra en la firma `SAPISIDHASH` |
| `CXP_API_KEY` | sí | API key de la consola (el `key=` de las peticiones) |
| `CXP_COOKIE` | sí | cabecera `Cookie` copiada del navegador |
| `CXP_SAPISID`, `CXP_SID`, `CXP_HSID`, `CXP_SSID`, `CXP_APISID` | alternativa | las cookies por separado |
| `CXP_SESSION_FILE` | alternativa | `storage_state` de Playwright del que extraerlas |
| `CXP_API_VERSION` | no (`v3beta1`) | versión del proto del backend interno |
| `CXP_DEFAULT_AGENT` | no | agente con el que se trabaja si no se indica |
| **`CXP_AGENT_READ`** | — | agentes con permiso de **lectura** |
| **`CXP_AGENT_READWRITE`** | — | agentes con permiso de **lectura y escritura** |
| `CXP_PROJECTS` | no | (opcional) limita los proyectos permitidos |
| `CXP_READ_ONLY_AGENTS` | no | (legacy) se une a `CXP_AGENT_READ` |

Se busca el `.env` en: `CXP_ENV_FILE` → la raíz del paquete → el directorio actual →
`~/.config/cx-playbook.env`. Las variables de entorno `CXP_*` tienen prioridad sobre
el fichero (**no se fusionan**: una variable de entorno *reemplaza* al valor del
`.env`).

### Permisos por agente (lista blanca)

Un agente **sin** entrada en `CXP_AGENT_READ` ni en `CXP_AGENT_READWRITE` **no es
accesible en absoluto** (ni para leer). Las entradas admiten dos formas:

- `<uuid>` → el permiso viaja con el uuid: vale en cualquier proyecto.
- `<proyecto>/<uuid>` → empareja proyecto y agente.

El proyecto se deduce del **path de la petición**, no de `CXP_PROJECT`: así el mismo
MCP puede leer un agente de PROD (que vive en otro proyecto) sin abrirle la escritura.

> **REGLA DURA:** `CXP_AGENT_READWRITE` debe contener **SOLO tu copia** del agente.
> Nunca el agente de referencia ni el de producción: editar un playbook que no sea
> tu copia rompería la paridad con la consola.

La guarda compara el **id resuelto** (no un alias) y se aplica en la **capa baja**
(`cxp/console_api.py`), no solo en la capa MCP: un `PATCH` que se cuele por
`cx_api` también pasa por el control de permisos. Concretamente:

- `ConsoleApi.request()` deduce el agente del **path** de la petición y aplica
  `assert_writable` a todo verbo de escritura (`POST/PATCH/PUT/DELETE`) y
  `assert_readable` al resto.
- `assert_playbook_writable()` exige `CXP_AGENT_READWRITE` para tocar un playbook.
- Ningún camino (ni el passthrough genérico) puede saltarse la lista blanca.

---

## Registro en un cliente MCP

```json
{
  "mcpServers": {
    "cx-playbook": {
      "command": "/ruta/a/mcp-cx-playbook/.venv/bin/python",
      "args": ["/ruta/a/mcp-cx-playbook/server.py"],
      "env": { "CXP_ENV_FILE": "/ruta/a/mcp-cx-playbook/.env" }
    }
  }
}
```

> OJO: el SDK de MCP **no hereda el entorno del proceso padre** (usa un subconjunto
> seguro: `PATH`, `HOME`…). Si el cliente no pasa `env`, el servidor solo ve lo que
> haya en `CXP_ENV_FILE`.

---

## Herramientas

### Espejo genérico

| herramienta | qué hace |
|---|---|
| `cx_status` | configuración, **tabla de permisos** y salud del backend (GET real) |
| `cx_discover` | el mapa de la API: sale del **discovery doc oficial** del backend (162 métodos / 32 recursos en v3beta1). `filter="flows"`, `method="agents.flows.train"` |
| `cx_api` | **cualquier** método de v3beta1, 1:1: `method="agents.playbooks.patch"` o `method="GET", path="agents/{agent}/flows"` |

`cx_api` cubre el 100 % de la superficie (los 162 métodos), incluidas las
operaciones que no están tipadas: `environments`, `deployments`, `experiments`,
`testCases`, `versions`, `export`/`import`, `operations`…

### Tipadas del núcleo

`cx_list_agents`, `cx_get_agent`, `cx_update_agent`, `cx_list_flows`,
`cx_get_flow`, `cx_update_flow`, `cx_list_pages`, `cx_get_page`,
`cx_update_page`, `cx_list_playbooks`, `cx_get_playbook`, `cx_update_playbook`,
`cx_list_children` (intents / entityTypes / tools / webhooks /
transitionRouteGroups / generators / changelogs / conversations),
`cx_get_conversation`, `cx_detect_intent`, `cx_train_flow`, `cx_validate_flow`.

### Los huecos de la API pública

| herramienta | qué hace |
|---|---|
| `cx_gaps` | **detecta en vivo** los campos que el backend expone y la API pública no declara. Trae además el catálogo medido (qué huecos se pueden escribir y cuáles son solo lectura) |
| `cx_set_console_field` | **escribe cualquier campo** (incluidos los fuera de esquema) por ruta: `transitionRoutes[0].targetPlaybook`, `form.parameters[0].description`, `entities[0].dtmfPattern`… Verifica releyendo y dice qué se guardó y qué se perdió |
| `cx_set_route_playbook` | destino a playbook de una transition route **de página** |
| `cx_set_flow_route_playbook` | destino a playbook de una transition route **del router de un flow** |
| `cx_set_group_route_playbook` | destino a playbook de una transition route **de un grupo de rutas** |

`cx_gaps` es la pieza que **no depende de esta lista**: recorre el agente, compara
cada recurso con el esquema público y devuelve lo que encuentra. Si mañana Google
añade un campo nuevo a la consola, sale en `cx_gaps` sin tocar el código.

`cx_set_console_field` es la pieza **genérica** de escritura: en vez de un método
por operación, hay una sola:

```
GET recurso  ->  se aplica el valor en la ruta  ->  PATCH  ->  GET y compara
```

La comparación final es la garantía: el servidor puede descartar un campo en
silencio (es lo que hace la API pública), y la respuesta lo dice
(`verificado`, `descartado_por_el_servidor`, `cambios`).

### Clonado 1:1 de un agente (el canal completo)

| herramienta | qué hace |
|---|---|
| `cx_clone_agent` | **limpia el destino y recrea el original 1:1**, con benchmark por acción |
| `cx_limites_api` | **la memoria del MCP**: los límites medidos de la API (síntoma → causa → rodeo) |
| `cx_net_probe` | dónde se va el tiempo de cada petición (send / cabeceras / cuerpo) |
| `cx_batch` | N llamadas agrupadas: el `/batch` del backend **no existe** (404) → **cae a hilos** y lo dice en `modo` |

`cx_clone_agent(origen, destino, modo="aplicar"|"plan"|"limpiar")`:

- `modo="aplicar"` (por defecto): snapshot del original → limpieza del destino
  → recreación 1:1 → verificación (conteos **y** firma de routing por página).
- `modo="plan"`: no escribe nada; dice qué se borraría y qué se crearía.
- `modo="limpiar"`: solo la limpieza del destino.

Devuelve el **benchmark**: `ms_total`, `ms_fases` (tiempo de reloj por fase),
`ms_suma_por_fase` (suma de las llamadas), `limpiar`, `crear`, `paridad`,
`omitidos`, `desviaciones` y **todas las acciones una a una con su tiempo**.

Solo escribe en el **destino**: el original es de solo lectura y la guarda lo
bloquea en la capa baja (`cxp/console_api.py`), también dentro de `cx_api`.

#### Orden de creación (lo dicta el servidor, no la conveniencia)

```
entityTypes, intents, webhooks, tools   (hojas)
  -> flows        (con las rutas que ya son resolubles; si apuntan a una
                   pagina o un playbook, van en un PATCH posterior)
  -> playbooks    (referencian tools y flows; se referencian ENTRE si)
  -> generators
  -> paginas      (orden topologico; los ciclos se rompen parcheando)
  -> PATCH de rutas de flow y de las paginas con destinos pendientes
  -> PATCH de ajustes del agente
```

Los ids se **remapean** (los del original no existen en el destino): cada
nombre de recurso se reescribe par a par (`recurso/id`), incluidos los
anidados (`.../flows/{flow}/pages/{page}` y el destino especial `END_FLOW`).

---

## Instrucciones para el agente que usa este MCP

> Copia este bloque en el prompt/sistema del agente que consume el MCP.

**Purpose.** Operar Dialogflow CX como un espejo de su API v3beta1, y además
escribir los campos que la API pública no puede expresar (destino a playbook
en una transición, descripciones de parámetros de formulario, patrones DTMF,
ajustes del cliente web) y **detectar** los que faltan.

**Antes de la primera llamada:** llama a `cx_status`. Si `credenciales_ok` es
`false` o `sesion_aceptada` es `false`, **no sigas**: las credenciales están mal
o la sesión de Google caducó. Avisa al usuario (no hay forma de arreglarlo desde
el MCP).

**Si vas a escribir mucho (o a clonar):** llama a `cx_limites_api()` **antes**.
Ahí está la memoria del MCP: los límites medidos de la API (síntoma exacto →
causa → rodeo) y las constantes de rendimiento. **No re-descubras lo que ya
está medido**: el descriptor oficial no los documenta.

- **El backend serializa las escrituras del mismo agente**: el rendimiento se
  estanca en ~1,5-2 ops/s sea cual sea la concurrencia. **Crea en serie.** El
  borrado de elementos independientes admite **2 en paralelo** (+20 %). Las
  **lecturas sí paralelizan** (8 GET ≈ 1 s).
- **Menos escrituras > escrituras más rápidas**: como el backend serializa, la
  única forma de bajar el tiempo total es **escribir menos**.
- **Verifica al final**, no tras cada escritura (cada GET extra son ~0,5 s).
- Si un error viene con `causa`/`workaround` en la respuesta, **ese es el rodeo
  ya conocido**: aplícalo en vez de reintentar a ciegas.

**Para clonar un agente:** usa `cx_clone_agent(origen, destino)`; lleva el
orden de dependencias, el remapeo de ids y los rodeos ya resueltos.

**Para escribir en un agente que no sea el de por defecto:** comprueba la tabla
`permisos` de `cx_status`. Un agente sin permiso devuelve un error claro
(`no tiene permiso`); uno de solo lectura, `SOLO LECTURA`. No busques rodeos.

**Cómo usar `cx_set_route_playbook` (y `cx_set_flow_route_playbook`, y
`cx_set_group_route_playbook`):**

1. **Primero lee.** Llama con `agent`, `flow` y **sin `route`**: devuelve el
   estado actual (cada ruta con su condición y su destino). Con esto eliges la
   ruta exacta.
2. **Identifica la ruta** por su `name` (uuid) o por su `condition` exacta. No uses
   índices si puedes evitarlo: son frágiles.
3. **Simula.** Llama con `route`, `playbook` y `dry_run=true`. Comprueba
   `destino_actual` vs `destino_nuevo` y el `aviso`: si la ruta apuntaba a una
   página o a un flow, ese destino **se borra** (son un `oneof`).
4. **Escribe.** Repite la llamada con `dry_run=false`.
5. **Verifica.** La respuesta incluye `verificado: true` y las rutas ya
   actualizadas. Si `verificado` es `false`, **dilo**: el cambio no está confirmado.

**Cómo usar `cx_set_console_field` (un campo fuera de esquema):**

1. **Descubre** el campo con `cx_gaps` (o mira el `catalogo`): te dice el
   recurso, el esquema y el estado (`escribible` / `derivado`).
2. **Simula** con `dry_run=true`: devuelve el diff previsto y el `updateMask`
   que se va a mandar.
3. **Escribe** con `dry_run=false`.
4. **Lee** el resultado: `verificado: true` y `descartado_por_el_servidor` (vacío
   = se guardó). Si el campo aparece en `descartado_por_el_servidor`, el servidor
   lo deriva: es de solo lectura.

**Reglas:**

- `playbook` se indica por **displayName**. Los ids de playbook son distintos en
  cada agente.
- Un cambio de destino **borra** `targetPage`/`targetFlow` de esa ruta (son un `oneof`).
  Si la ruta tenía destino a página o flow, eso se pierde: avisa antes.
- El MCP escribe el **array completo** de rutas del recurso en un solo `PATCH`. Si otro
  proceso edita el mismo flow o página a la vez, tu escritura pisa su cambio. No edites
  el mismo recurso en paralelo.
- Cada llamada re-firma la petición (`SAPISIDHASH` con el timestamp actual). No hace
  falta refrescar nada.
- Los `targetPlaybook` no se ven por la API pública: para comprobar el resultado usa el
  `rutas` que devuelve la propia herramienta, no un `GET` público.
- **Usa `v3beta1`, nunca `v3`**.

**Ejemplo de flujo:**

```
cx_status()
  -> credenciales_ok: true, sesion_aceptada: true
  -> permisos: [{agente: <uuid>, permiso: lectura/escritura}, ...]

cx_discover(filter="playbook")
  -> total_metodos: 162

cx_gaps(agent="<uuid>")
  -> total_huecos: 25
  -> huecos: [{esquema: "Tool", campo: "actionSchemas", ...}, ...]
  -> catalogo: [{campo: "transitionRoutes[].targetPlaybook", estado: "escribible"}, ...]

cx_get_flow(agent="<uuid>", flow="Mi_Flujo")
  -> rutas: [{name: "…", condition: "$session.params.canal = whatsapp",
              target_kind: "targetPage", target_name: "26d926f7-…"}, …]

cx_set_flow_route_playbook(agent="<uuid>", flow="Mi_Flujo",
                            route="<uuid-de-la-ruta>",
                            playbook="mi_playbook_destino", dry_run=true)
  -> destino_actual_nombre: mi_playbook_anterior
     destino_nuevo_nombre:  mi_playbook_destino

(cx_set_flow_route_playbook(..., dry_run=false))
  -> verificado: true
```

---

## Límites medidos de la API

`cx_limites_api()` los devuelve con el **síntoma exacto**, la causa y el rodeo;
el pipeline de clonado **anota solo** cada error con su causa y su rodeo. Lo
que la API **no** documenta (el descriptor oficial no lo dice):

| id | lo que pasa | rodeo |
|---|---|---|
| `delete_force` | `DELETE` de un recurso referenciado → 400 *"Please set 'force' to true"* | `force=true` en flows/tools/webhooks/entityTypes/generators/transitionRouteGroups (**no** en playbooks ni intents) → borrar por orden de dependencias |
| `escrituras_serializadas` | el rendimiento se estanca: POST 1→1,57 · 2→1,01 · 3→1,03 · 4→1,34 · 6→1,14 · 8→1,12 ops/s | **crear en serie**; el **borrado** sí admite **2 en paralelo** (+20 %) |
| `batch_404` | `/v3beta1/batch` → **404** en el backend interno | `cx_batch` cae a **hilos**; nunca cuentes con `/batch` |
| `latencia_por_tamano` | +50 KB de cuerpo ≈ +600 ms | no duplicar viajes: verificar **al final**, no tras cada escritura |
| `flow_ignora_pages` | `POST flow` ignora las `pages` del cuerpo | un POST por página |
| `destinos_validados` | 400 *"Target page … does not exist in the flow"* / *"Referenced resource X does not exist"* | crear en orden de dependencias; en los ciclos, crear sin rutas y parchear después |
| `ciclos_paginas` | en un ciclo de páginas, la ruta no puede validarse al crearla | ordenar las páginas para romper los ciclos: **1 PATCH extra por página del ciclo** |
| `rutas_en_el_post` | un flow creado sin rutas y luego parcheado = 2 escrituras | si **todas** sus rutas ya son resolubles, meterlas en el `POST` del flow |
| `end_flow` | el destino de fin de flow es `.../flows/{flow}/pages/END_FLOW` | remapear **cada** par `recurso/id` del nombre |
| `playbook_loop` | 400 *"Playbook X caused loop in playbook routes"* | un playbook autorreferenciado no se puede crear ni actualizar por API |
| `macros_validadas` | las macros `${PLAYBOOK:x}`/`${FLOW:x}`/`${TOOL:x}` del texto se validan | crear los playbooks en orden topologico |
| `campos_solo_lectura` | `referencedFlows`/`referencedPlaybooks`/`inlineActions` se derivan; `defaultLanguageCode` no se puede actualizar | no mandarlos al escribir |
| `campos_fuera_de_esquema` | un PATCH de un campo que la API pública no declara devuelve 200 y **el campo no está** | escribir contra el backend interno y **verificar releyendo** (`cx_set_console_field`) |
| `patch_etag_obsoleto` | PATCH con el `etag` viejo → **409** *"Others may have made recent changes…"* | quitar el `etag` del cuerpo (la consola tampoco lo manda) |
| `ruta_sin_intent_ni_condition` | 400 *"At least one of intent or condition in transition route should be specified"* | poner siempre `condition` o `intent` en la ruta |
| `ruta_sin_destino` | 400 *"At least one of trigger fulfillment, target page, target flow, or target playbook…"* (en grupos de rutas) | dar destino al crear el grupo |
| `subcampos_desconocidos` | escribir `advancedSettings` vacía sus subcampos desconocidos | son de solo lectura (`estado: derivado` en el catálogo) |
| `action_schemas_derivadas` | escribir `Tool.actionSchemas` no cambia nada | solo lectura: cambiar el `openApiSpec`, no los esquemas |
| `tipos_no_declarados_se_validan` | 400 *"Invalid value at 'agent.web_client_settings' (TYPE_STRING)"* | copiar el valor que ya tiene el recurso y cambiar solo el tipo |
| `tool_builtin` | los tools `df-*` no se pueden crear ni borrar | saltarlos (ya existen en todo agente) |
| `tool_openapi_vacio` | 400 *"Null or empty definition"* (tools duplicados sin `textSchema`) | recrearlos **sin** `openApiSpec` (desviación documentada) |
| `tool_datastore_borrado` | 400 *"Failed to connect datastores … do not exist"* | recrear **sin** `dataStoreSpec` (desviación documentada) |
| `start_flow_protegido` | no se puede borrar el `Default Start Flow` ni vaciar sus handlers | no borrarlo: PATCH de `transitionRoutes` y nada más |
| `guard_metodo_propio` | `agents/{id}:export` se rechazaba ("no tiene permiso") | corregido en `cxp/guard.py` (se corta el sufijo `:método`) |

---

## Detalles técnicos (medidos)

### Red

| riesgo | mitigación | dónde |
|---|---|---|
| Handshake TLS en cada petición (0,66 s) | **una sola `Session` para todo el servidor** → keep-alive por hilo | `server.py` (`_HTTP`/`_API`/`_MIRROR`) |
| IP negra entre las de `/etc/hosts` | **timeout de connect de 8 s** (no 45) → cae rápido a la siguiente IP | `cxp/httpclient.py` |
| Payload enorme (`list_playbooks` puede ser cientos de KB) | **caché de catálogos** (id→displayName) con TTL 120 s y autorrenovación | `cxp/console_api.py` |
| Error de red transitorio | 1 reintento, **solo de red** (un HTTP 4xx/5xx no se reintenta) | `cxp/httpclient.py` |
| Timeout de lectura | 45 s (300 s para `detectIntent`/`train`/`validate`) | `cxp/config.py` |
| MTU / offloads / DNS del sistema | **no se tocan**: el MCP no modifica el estado del sistema | — |

Medición del antes/después de la llamada típica (`cx_get_flow`):

| | por llamada |
|---|---|
| antes (Session nueva por llamada + sin caché) | **2,663 s** |
| después (keep-alive + caché de catálogos) | **0,402 s** |

### El backend interno sirve las tres versiones

`dialogflow.clients6.google.com` responde en `/v3alpha1`, `/v3beta1` y `/v3`, y
en las tres entrega su **discovery doc** (`$discovery/rest?version=…`). Por eso el
espejo puede ser "de v3beta1" sin escribir los 162 métodos a mano: el mapa sale del
descriptor.

**Autenticación de primera parte.** Cabeceras mínimas que el backend acepta (verificadas
por captura, no de memoria):

```
authorization: SAPISIDHASH {ts}_{h} SAPISID1PHASH {ts}_{h} SAPISID3PHASH {ts}_{h}
cookie:        las 5 cookies de sesion de `.google.com`
x-origin:      <CXP_CONSOLE_ORIGIN>
x-referer:     <CXP_CONSOLE_ORIGIN>
x-goog-authuser: 0
referer:       https://dialogflow.clients6.google.com/static/proxy.html?usegapi=1
```

- `h = sha1(f"{ts} {valor_de_SAPISID} {console_origin}")`, `ts` = epoch actual. Los tres
  hash son iguales (la consola usa `SAPISID` para los tres). **Se re-firma en cada
  llamada** (delta medido: 0 s).
- **No mandar cabecera `Origin`**: el servidor exige que `Origin` y `Referer` coincidan
  → HTTP 400 *"Origin and Referer header don't match"*.
- Con `Bearer` (token de servicio) el backend responde **404 "Method not found"**: exige
  primera parte.
- HTTP/1.1 vale: no hace falta h2.
- Solo viajan las **5 cookies** del dominio `.google.com`: una cookie de otro dominio
  invalida la credencial (401).

**Escritura.** `PATCH .../flows/{FLOW}?updateMask=transitionRoutes` con el **array
completo** de `transitionRoutes` en el body (igual que la consola). El `etag` se omite a
propósito: el `GET` interno no lo devuelve y un etag viejo hace fallar el `PATCH`.

**Trampas:**

| síntoma | causa |
|---|---|
| HTTP 404 "Method not found" | se usó `Bearer` en vez de primera parte |
| HTTP 401 "invalid authentication credentials" | faltan cookies, o se mandaron cookies de otros dominios |
| HTTP 400 "Origin and Referer don't match" | se mandó la cabecera `Origin` |
| El cambio no aparece | el `PATCH` no incluye `targetPlaybook` (proto público) o el flow no es el que creías |
| `no tiene permiso` | el agente no está en `CXP_AGENT_READ` / `CXP_AGENT_READWRITE` |
| El subproceso MCP no ve tus variables | el SDK de MCP no hereda el entorno: pásalo en `env` |

---

## Desarrollo

```bash
pip install -e ".[dev]"

pytest                  # tests unitarios (sin red)
python smoke_test.py    # prueba de punta a punta por el protocolo MCP
```

* **`tests/`** — tests unitarios de la capa de huecos y de la escritura genérica
  (no necesitan red ni credenciales).
* **`smoke_test.py`** — llama a las herramientas de verdad por el protocolo MCP.
  Es **genérico**: no hay ningún agente, flow ni playbook cableado; descubre
  todo en el agente del `.env` y escribe solo sobre recursos **temporales**
  (flow, página, entity type y grupo de rutas) que borra al final.
* **`probes/`** — las sondas que produjeron las mediciones: el detector de
  huecos (`probe_schema_gaps.py`), la matriz de escritura (`probe_console_only_writable3.py`)
  y los experimentos de captura de tráfico de la consola. No hacen falta para
  operar. Los ids de agente que usan salen del entorno
  (`CXP_PROBE_AGENTS`, `CXP_PROBE_TARGETS`), nunca cableados.

---

## Aviso legal

Este proyecto **no está afiliado a Google**. Habla con un backend interno de
Google que no forma parte de la API pública y usa la autenticación de primera
parte de tu propia cuenta.

* Úsalo solo con cuentas y proyectos tuyos, y solo para lo que la consola ya
  te permite hacer a mano.
* Las cookies de sesión de tu navegador son **credenciales**: trátalas como
  una contraseña (no las subas a un repositorio, no las compartas).
* Los datos que escribe este MCP son los mismos que escribes en la consola:
  el MCP no inventa capacidades, solo usa un camino que la API pública no
  expone.
* Las APIs de Google y sus políticas pueden cambiar en cualquier momento.

Licencia: [Apache-2.0](LICENSE).
