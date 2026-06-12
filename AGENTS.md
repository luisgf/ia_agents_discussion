# Agents Discussion — Memoria del proyecto

Plataforma de diagnóstico técnico multiagente para incidencias de producción.
Orquesta un debate estructurado entre tres agentes LLM sobre un problema técnico,
con herramientas ReAct de inspección de infraestructura y supervisión humana opcional.

---

## Stack tecnológico

| Capa | Tecnología |
|---|---|
| Orquestación de agentes | LangGraph (`StateGraph`) |
| Modelos LLM | `langchain-openai` → GitHub Models / GitHub Copilot |
| API web | FastAPI + SSE (Server-Sent Events) |
| Frontend | Vanilla JS, CSS, HTML — sin build pipeline |
| Persistencia | JSON files en `~/.local/share/agents-discussion/runs/` |
| Config | `pydantic-settings` + `.env` |
| Python | 3.11+ |

---

## Estructura de archivos (src/agents_discussion/)

```
graph.py          — Definición del grafo LangGraph, nodos agentes, routing
state.py          — TypedDict DebateState + modelos Pydantic (Hypothesis, ModeratorDecision…)
web.py            — FastAPI routes, SSE streaming, RunStore, RunSession
models.py         — Factory ChatOpenAI para GitHub Models / Copilot
pricing.py        — Tabla de precios y estimación de coste por tokens (USD/1M)
prompts.py        — Construcción de prompts por agente (funciones puras)
prompt_store.py   — Carga de plantillas YAML (built-in + custom)
config.py         — Pydantic-settings, env vars con aliases
tools.py          — @tool: SSH, kubectl, HTTP, Prometheus, Loki, Elasticsearch, DB EXPLAIN, git
runtime.py        — RunControl: cancelación, aprobación de tools, HITL
report.py         — Generación de informe Markdown desde run record
audit.py          — audit.jsonl append-only por invocación de tool
cli.py            — CLI con argparse + Rich
static/           — Frontend: index.html, css/app.css, js/app.js
prompt_templates/ — YAML built-in (default, performance, errors, data, security)
```

---

## Topología del grafo (LangGraph)

```
START → diagnostic_agent → skeptic_agent → diagnostic_rebuttal_agent
      → moderator_agent → summarize_history → [user_input_gate] → (loop)
                                                                 → finalize → END
```

- **diagnostic_agent**: hipótesis principal + tools ReAct (modelo: `diagnostic_model`)
- **skeptic_agent**: falsifica hipótesis + tools ReAct (modelo: `skeptic_model`)
- **diagnostic_rebuttal_agent**: responde al escéptico + refina hipótesis (reutiliza `diagnostic_model`)
- **moderator_agent**: decide continuación/cierre + `flow_directive` para siguiente ronda (modelo: `moderator_model`)
- **summarize_history**: comprime history antigua en rounds > 2 (modelo: `summary_model`, por defecto = `moderator_model`)
- **user_input_gate**: bloquea opcionalmente entre rondas para comentarios del operador

### Routing

- `moderator_agent` → `finalize` si: status ∉ {continue, needs_more_data} O round > max_rounds
- `moderator_agent` → `summarize_history` si: continúa
- `summarize_history` → `user_input_gate` si: `pause_between_rounds=True`
- `summarize_history` → `diagnostic_agent` otherwise

---

## Estado principal (DebateState)

Campos clave del TypedDict (ver `state.py` para la definición completa):

```python
topic, context          # Problema y contexto de entrada
round, max_rounds       # Contador de ronda actual y máximo
diagnostic_model        # Nombre del modelo asignado al diagnóstico
skeptic_model           # Modelo del escéptico
moderator_model         # Modelo del moderador
summary_model           # Modelo para comprimir history (vacío = usa moderator_model)
history                 # Lista[DebateMessage] con Annotated reducer (append)
token_usage             # Dict[agent_node, {input_tokens, output_tokens, total_tokens}] — reducer _merge_usage
hypotheses              # Lista[Hypothesis] con reducer merge-by-id (_merge_hypotheses)
round_log               # Lista[DebateRound] con reducer append
tool_calls_log          # Lista[ToolCallEntry] con reducer append
moderator_decision      # ModeratorDecision | None (último veredicto del moderador)
early_out_recommended   # bool — señal del agente diagnóstico
run_id                  # str — enlaza nodos con RunControl para aprobación/cancelación
compress_history        # bool — activa la compresión de history
```

---

## Configuración activa (`.env`)

```env
DIAGNOSTIC_MODEL=copilot/gpt-4o
SKEPTIC_MODEL=copilot/claude-sonnet-4.6
MODERATOR_MODEL=copilot/claude-sonnet-4.6
MAX_ROUNDS=10
MAX_TOOL_CALLS_PER_AGENT=50
CONFIDENCE_THRESHOLD=0.8
GITHUB_MODELS_BASE_URL=https://models.github.ai/inference
```

Tokens de autenticación (GITHUB_TOKEN / COPILOT_TOKEN) configurados por separado, no documentados aquí.

### Variables relevantes adicionales

```env
SUMMARY_MODEL=            # Vacío → usa MODERATOR_MODEL
COMPRESS_HISTORY=true
EARLY_OUT_THRESHOLD=0.9
TOOL_APPROVAL_REQUIRED=true
APPROVAL_REQUIRED_TOOLS=run_ssh_command,run_local_command,run_kubectl,run_db_explain
MODEL_PRICES_FILE=        # Opcional: JSON con precios USD/1M para estimación de coste
DATA_DIR=~/.local/share/agents-discussion/runs
```

---

## Flujo de persistencia

1. `POST /api/runs` → crea `meta` con `run_id`, `timestamp` (= inicio), modelos
2. `RunStore.create_stub()` → escribe JSON stub en `DATA_DIR/{run_id}.json` con `status: "running"`
3. `asyncio.to_thread(_run_debate_sync)` → hilo worker ejecuta el grafo
4. `_run_debate_sync` itera `stream_debate_events()` → publica cada evento en `RunSession.events`
5. Al finalizar `_drive_run.finally`: calcula `finished_at`, `duration_seconds`, extrae `token_totals`/`cost_estimate`
6. `RunStore.save()` → escribe JSON final atómicamente (temp + rename)

Campos en el JSON persistido:
```json
{
  "run_id", "topic", "timestamp", "finished_at", "duration_seconds",
  "status", "models", "reasoning_effort", "template", "language",
  "parent_run_id", "token_totals", "cost_estimate", "context", "events"
}
```

`_EPHEMERAL_EVENTS` (no persistidos): `agent_turn_started`, `agent_delta`

---

## Captura de tokens

Implementada en `graph.py`. Cada llamada LLM retorna `usage_metadata`:

- **Agentes con tools** (`_run_with_tools`): acumula `usage_metadata` por iteración del bucle ReAct;
  devuelve `(content, tool_log, usage_dict)`. Los nodos añaden `{"token_usage": {node_name: usage_dict}}` al estado.
- **Streaming** (`_invoke_streaming`): `stream_usage=True` en `model.stream()`.
- **Moderador**: `with_structured_output(ModeratorDecision, include_raw=True)` para capturar tokens del output estructurado.
- **Summary**: directamente desde `summary_response.usage_metadata`.
- `stream_debate_events()` acumula de los updates del grafo y emite en `run_finished`:
  `{"type": "run_finished", "token_totals": {...}, "cost_estimate": {...}}`

### Estimación de coste (`pricing.py`)

- Tabla de precios por defecto para ~30 modelos (OpenAI, Anthropic, Google, Meta, Mistral, Phi, Cohere)
- `_normalize_name(model)`: strips prefixes (`copilot/`, `openai/`…), normaliza separadores a `-`
- `_find_price(model, prices)`: lookup exacto + fuzzy por subcadena más larga
- `estimate_cost(token_usage, models_by_role, prices_file)` → `{by_node, total_usd, has_prices}`
- Personalizable via `MODEL_PRICES_FILE` (.env) → JSON `{"modelo": {"input": X, "output": Y}}`

---

## Convenciones de código

- **Formatter/Linter**: Ruff (target-version = py311, line-length = 120)
- **Pre-commit obligatorio**: `ruff check .` + `ruff format --check .` + `python -m py_compile *.py`
- **Imports**: stdlib → third-party → first-party (absolutos, no relativos)
- **Type hints**: requeridos en todas las firmas; `str | None` (no `Optional[str]`)
- **Logging**: `_log = logging.getLogger(__name__)`, no `print()`
- **Errores LLM**: `except Exception as exc:  # noqa: BLE001` + `_log.warning`
- **Node returns**: dicts parciales (solo los campos modificados)
- **Frontend JS**: `const`/`let`, no `var`; siempre `esc()` para texto usuario, `md()` para markdown
- **Nuevos campos de estado**: añadir en `state.py` con el reducer apropiado; inicializar en `create_initial_state()`
- **Tests**: `tests/` con pytest (`.venv/bin/python -m pytest`); lógica pura sin LLM (modelos stub, monkeypatch de `create_github_model`/`get_settings`)

---

## Patrones importantes

### Añadir un nuevo campo al estado

1. Añadir a `DebateState` en `state.py` con su reducer si es acumulable
2. Inicializar en `create_initial_state()` en `graph.py`
3. Si debe persistirse en la lista del historial: añadir a `list_runs` `keys` en `web.py:89`
4. Si debe estar en el record final: incluir en `RunSession.record()` o en el registro extra

### Añadir una nueva tool

1. Definir en `tools.py` con `@tool`
2. Añadir a `APPROVAL_REQUIRED_TOOLS` si es sensible
3. Los agentes la ven automáticamente si `tools_enabled=True` y el nombre está en `enabled_tool_set`

### Añadir un nuevo tipo de evento SSE

1. Emitir desde `stream_debate_events()` (graph.py) o `RunControl.emit()` (runtime.py)
2. Manejar en `renderEvent(ev)` en app.js
3. Si no es efímero, se persiste automáticamente en el JSON del run

### Skips de agentes

Los nodos `skeptic_agent` y `diagnostic_rebuttal_agent` comprueban `_should_skip()` al inicio.
Las skips se implementan como passthrough (retornan placeholder), no como reconstrucción del grafo.

---

## Estructura del JSON de run (eventos relevantes)

```
run_started         → {topic, max_rounds, confidence_threshold, template, language}
agent_turn_started  → {agent_node, agent_role}                    [EFÍMERO]
agent_delta         → {agent_node, agent_role, delta}             [EFÍMERO]
agent_completed     → {node, role, content}
agent_reasoning     → {agent_node, agent_role, content}
agent_skipped       → {node, role, rationale}
tool_call_started   → {call_id, agent_node, tool_name, args}
tool_call           → {call_id, tool_name, args, result, error, approval, cached, duration_ms}
                      approval ∈ auto|approved|rejected|timeout|cached (cached = servido del ToolCache, sin re-ejecución)
tool_approval_request → {tool_name, args, agent_role}
tool_approval_resolved → {approved, approval}
moderator_decision  → {node, decision: ModeratorDecision, round}
hypothesis_update   → {node, round, hypotheses: [Hypothesis…]}  (snapshot completo deduplicado)
history_compressed  → {round, summary}
awaiting_user_input → {round}
user_comment        → {content}
final_result        → {node, content}
run_finished        → {token_totals, cost_estimate}
run_cancelled       → {}
error               → {message}
```

---

## API REST

```
POST   /api/runs                    Iniciar debate (multipart/form-data)
GET    /api/runs                    Listar runs (proyección sin context/events)
GET    /api/runs/{id}               Run completo con events
GET    /api/runs/{id}/events        SSE stream en vivo
DELETE /api/runs/{id}               Eliminar
POST   /api/runs/{id}/resume        Reanudar con nueva evidencia
POST   /api/runs/{id}/cancel        Cancelar
POST   /api/runs/{id}/approval      Resolver aprobación de tool
POST   /api/runs/{id}/comment       Comentario HITL entre rondas
GET    /api/models                  Modelos disponibles (catálogo)
GET    /api/prompts                 Plantillas disponibles
GET    /api/settings                Configuración actual
GET    /api/runs/{id}/report        Informe Markdown
```

---

## Cambios recientes implementados

### Historial de debates: tiempos y duración
- `RunSession` registra `started_at` (= `meta["timestamp"]`), `finished_at`, `duration_seconds`
- Tabla del historial: nueva columna **Duración** + subtítulo `~Xk tok` cuando hay datos
- Detalle del run: barra `.run-timing-bar` con Inicio / Fin / Duración debajo del banner de replay
- Informe Markdown: líneas **Inicio**, **Fin**, **Duración** en la cabecera

### Bug modelos usados (2 → 3)
- `app.js` `renderHistoryList`: array ahora incluye `diagnostic + skeptic + moderator` (antes faltaba moderator)

### Consumo de tokens + estimación de coste
- Nuevo módulo `pricing.py` con tabla de precios y `estimate_cost()`
- `DebateState.token_usage` con reducer `_merge_usage` que acumula por nodo
- Captura en todos los puntos: streaming (`stream_usage=True`), moderador (`include_raw=True`), summary (`.usage_metadata`)
- `run_finished` event incluye `token_totals` y `cost_estimate`
- Frontend: `buildTokenStatsCard()` en detalle del run
- Informe Markdown: sección **Consumo de tokens** con tabla por agente y coste estimado
- `.env`: nueva variable `MODEL_PRICES_FILE` documentada

### Mockup: mapa interactivo de hipótesis
- **`static/vendor/cytoscape.min.js`** — Cytoscape.js 3.30.2 vendorizado (patrón = marked/purify)
- **`static/mockup-hypotheses.html`** — página autónoma de demostración, accesible en `/static/mockup-hypotheses.html`
  - 6 hipótesis de ejemplo con los 3 estados (`active`/`confirmed`/`rejected`), 3 rondas, evidencias y transiciones
  - **Vista radial** (layout `cose`): nodo PROBLEMA → agentes → hipótesis; aristas azul=propuso, verde=confirmó, rojo-discontinuo=refutó
  - **Vista línea de tiempo** (layout `preset`): columnas por ronda, filas por hipótesis, evolución de estados
  - Filtro de ronda (All/1/2/3), botón ▶ Reproducir (revela nodos ronda a ronda simulando SSE), panel lateral con detalle completo e historial de transiciones
  - Paleta heredada de `app.css` (variables `--diag`, `--final`, `--err`, etc.)

## Modelo de hipótesis (implementación real)

El mapa de hipótesis está integrado en la UI real (pestaña **Mapa**); el mockup queda como referencia de diseño.

### Modelo `Hypothesis` (`state.py`)

```python
class HypothesisTransition(BaseModel):    # serializar SIEMPRE con model_dump(by_alias=True)
    round: int
    from_state: str | None   # alias "from"; None = creación
    to_state: str             # alias "to"
    agent: str
    note: str

class Hypothesis(BaseModel):
    id: str                # id crudo del LLM (HYPOTHESIS-<n>); clave canónica de merge
    text: str
    state: Literal["active", "rejected", "confirmed"]
    proposer: str          # siempre "diagnostic_agent" en la práctica
    round: int             # ronda de CREACIÓN (las transiciones llevan su propia ronda)
    probability: float | None  # P estimada por los agentes (formato "### HYPOTHESIS-n [P=0.6]"); clamp en extracción
    supporting_evidence: list[str]   # líneas [tool:...] del bloque de la hipótesis
    rejected_reason: str | None
    transitions: list[HypothesisTransition]
```

El escéptico recalibra P por hipótesis (`[P=<0-1>]` junto al id en su respuesta, parseado en
`skeptic_agent`); en el merge, P entrante `None` conserva la anterior.

### Diseño implementado

- **Reducer `_merge_hypotheses`** (state.py): merge by id, orden de primera aparición; conserva `round`/`proposer` originales, toma `state`/`text`/`rejected_reason` entrantes, union de evidencias, dedup de transiciones por `(round, to_state, agent)` descartando creaciones re-emitidas.
- **IDs estables**: el prompt del diagnóstico recibe las hipótesis en debate (`diagnostic_prompt(..., hypotheses=...)`) e instruye a reutilizar ids exactos y continuar la numeración. El fallback sin formato usa id round-scoped `R{n}-F1` para no fusionar fallbacks de rondas distintas.
- **Evidencias**: `_split_hypothesis_block` (graph.py) separa el texto de la hipótesis de las líneas `[tool:...]` (máx. 5, 250 chars c/u).
- **Transiciones**: creación en `_extract_hypotheses`; cambios de estado en `skeptic_agent` (que ahora devuelve **solo** las hipótesis modificadas; el reducer fusiona).
- **Evento SSE `hypothesis_update`**: emitido en `stream_debate_events()` tras el `agent_completed` de cualquier nodo cuyo update traiga `hypotheses`; payload = snapshot completo deduplicado (`model_dump(by_alias=True)`, reconstruido con el propio reducer porque los updates del stream son parciales). No es efímero → se persiste y el replay reconstruye el mapa.

### Exclusión deliberada

`diagnostic_rebuttal_agent` no actualiza hipótesis: su salida es texto libre sin formato parseable. La emisión SSE es genérica (cualquier `node_update` con `hypotheses`), así que incorporarlo solo requiere que el nodo devuelva hipótesis.

### Frontend: pestaña Mapa

- **`static/js/hypothesis-map.js`** — módulo IIFE que expone `window.HypoMap` con `setTopic / update / setDecision / reset / show`.
- Layout `concentric` determinista: topic al centro, anillo = ronda de creación. Render incremental (`cy.add()` / `node.data()`, sin destroy+rebuild); `fit` solo en el primer layout para no robar el zoom al usuario.
- Lazy-init obligatorio: el contenedor nace oculto (Cytoscape mediría 0×0); `show()` crea/resizea y sincroniza si hay updates pendientes (dirty flag).
- Filtro de ronda dinámico (clase `.dimmed`), panel lateral de detalle (evidencias, motivo de rechazo, historial de transiciones), tooltip con texto completo, replay de transiciones de estado (no solo apariciones).
- Hipótesis líder: Jaccard de tokens entre `moderator_decision.leading_hypothesis` y `h.text`; score ≥ 0.3 marca el nodo con halo `.leader`; siempre se muestra la franja superior con el texto del moderador.
- Wiring en `app.js`: rama `hypothesis_update` en `renderEvent` (early-return, antes de `closeToolGroup`), `setTopic` en `run_started`, `setDecision` en `moderator_decision`, `reset` en `clearThread()`, `show` al activar la pestaña.

---

## Capacidad diagnóstica y economía de tokens

### Prompting metodológico (templates v2)

Los 10 YAML (`prompt_templates/{default,performance,errors,data,security}.{es,en}.yaml`, `version: 2`)
comparten un bloque común marcado con `# --- Metodología v1 ... ---` (replicado, NO compuesto:
los archivos son autocontenidos para no romper overrides custom; mantener sincronizado a mano):

- `diagnostic_system` → bloque "Metodología": cronología primero (git_recent_changes + correlación),
  diagnóstico diferencial (≥2 alternativas), priorización información/coste, guía herramienta→señal.
  En `performance` se omite el bullet de cronología (su "Foco" ya lo cubre).
- `skeptic_system` → test discriminante entre hipótesis competitivas + no re-abrir rechazadas sin evidencia nueva.
- `moderator_system` → escala interpretativa de confianza (<0.3 conjetura / 0.3-0.6 plausible /
  0.6-0.8 probable / >0.8 confirmada) + usar las P de los agentes como input.

### ToolCache (runtime.py)

Caché por run de resultados de tools, compartida entre agentes: `RunControl.tool_cache`
(CLI sin control → caché local efímera por bucle). Hit solo si **misma ronda** y edad ≤ 300 s;
solo se cachean ejecuciones con `error=False` y approval auto/approved. Un hit no re-ejecuta ni
re-pide aprobación; se registra con `approval="cached"` (audit + tool_calls_log + evento `tool_call`
con `cached: true`, badge «caché» en la UI). El resultado servido lleva el prefijo
`[cached: ya ejecutado por <rol> en ronda <n>]` visible para el LLM.

### Reducción de redundancia en prompts

- `_history_before_current_round` (graph.py): skeptic/rebuttal/moderator reciben el history SOLO
  hasta la ronda anterior (+ comentarios `role=="user"` de la actual) — sus respuestas de la ronda
  en curso ya van explícitas en el prompt. El diagnóstico recibe history completo.
  OJO: el rol del rebuttal en history es `"diagnostic_rebuttal"` (sin `_agent`).
- **Compresión desde ronda 2** (`_history_mode`): comprimido si hay summary y round ≥ 2. En modo
  comprimido el "history" que se pasa a los prompts es solo la cola tras el último moderador
  (comentarios HITL); `format_history` renderiza summary + esa cola.
- `summarize_history`: la ronda terminada se deriva del history (`finished_round = nº de mensajes
  moderator`), NO de `state["round"]` (el moderador ya lo incrementó con `continue` — bug histórico
  que impedía generar el resumen). El resumen es **acumulativo**: integra el summary previo con los
  mensajes nuevos en ≤400 palabras.
- Los tres `*_deliver` (prompts.py) piden ~600 palabras máximo y citar tools resumidas.
- `_truncate` (tools.py) conserva 2/3 cabeza + 1/3 cola del output (antes cortaba solo el principio).

---

## Documentación de referencia del proyecto

| Documento | Contenido |
|---|---|
| `ARCHITECTURE.md` | Diseño detallado, schema de estado, topología, decisiones |
| `CODING_STYLE.md` | Convenciones Python y JS, patrones LangGraph |
| `OPERATIONS.md` | Despliegue, configuración completa, troubleshooting |
| `USAGE.md` | CLI, web UI, API, plantillas, tools |
