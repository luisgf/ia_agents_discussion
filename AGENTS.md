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
hypotheses              # Lista[Hypothesis] con reducer append
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
tool_call           → {call_id, tool_name, args, result, error, approval, duration_ms}
tool_approval_request → {tool_name, args, agent_role}
tool_approval_resolved → {approved, approval}
moderator_decision  → {node, decision: ModeratorDecision, round}
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

## Modelo de hipótesis (estado actual)

Relevante para la futura integración del mapa interactivo en la UI real.

### Modelo `Hypothesis` (`state.py`)

```python
class Hypothesis(BaseModel):
    id: str                # e.g. "1", "2" (el LLM emite HYPOTHESIS-<n>)
    text: str
    state: Literal["active", "rejected", "confirmed"]
    proposer: str          # siempre "diagnostic_agent" en la práctica
    round: int             # ronda de creación (NO se actualiza en transiciones)
    supporting_evidence: list[str]   # ⚠ siempre vacío — nunca se puebla
    rejected_reason: str | None
```

### Limitaciones conocidas (deuda técnica para la implementación real)

| Problema | Impacto |
|---|---|
| **Las hipótesis no se emiten por SSE ni se persisten** | El frontend no las conoce; solo ve `leading_hypothesis`/`rejected_hypotheses` como strings sueltos del moderador |
| **Reducer `_append_list` duplica** | El escéptico devuelve copias mutadas que se appendean; la lista acumula versiones obsoletas |
| **IDs inestables entre rondas** | El LLM reinicia numeración cada ronda; `H-1` de ronda 1 ≠ `H-1` de ronda 2 |
| **`round` no se actualiza en transiciones** | No se puede saber cuándo cambió de estado, solo cuándo se creó |
| **Sin aristas explícitas** | No existen relaciones `refuta`/`corrobora`/`deriva-de` entre hipótesis |
| **`supporting_evidence` siempre vacío** | Nunca se puebla en ningún nodo |
| **`diagnostic_rebuttal_agent` no actualiza hipótesis** | Las refinaciones del rebuttal no llegan al modelo estructurado |

### Plan de implementación real (cuando se decida avanzar)

1. Arreglar reducer: reemplazar `_append_list` por un reducer merge-by-id en `hypotheses`
2. Estabilizar IDs: prefijo con ronda (`R1-H1`) o hash del texto en `_extract_hypotheses`
3. Actualizar `round` en transiciones dentro del escéptico
4. Emitir evento SSE `hypothesis_update` desde `stream_debate_events()` (no efímero → persiste)
5. Poblar `supporting_evidence` desde el texto del diagnóstico/escéptico
6. Integrar la vista como pestaña en `index.html` / `app.js` consumiendo los nuevos eventos

---

## Documentación de referencia del proyecto

| Documento | Contenido |
|---|---|
| `ARCHITECTURE.md` | Diseño detallado, schema de estado, topología, decisiones |
| `CODING_STYLE.md` | Convenciones Python y JS, patrones LangGraph |
| `OPERATIONS.md` | Despliegue, configuración completa, troubleshooting |
| `USAGE.md` | CLI, web UI, API, plantillas, tools |
