# Agents Discussion — Project memory

Multi-agent technical diagnosis platform for production incidents.
It orchestrates a structured debate between three LLM agents about a technical
problem, with ReAct tools for infrastructure inspection and optional human oversight.

---

## Technology stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph (`StateGraph`) |
| LLM models | `langchain-openai` → GitHub Models / GitHub Copilot |
| Web API | FastAPI + SSE (Server-Sent Events) |
| Frontend | Vanilla JS, CSS, HTML — no build pipeline |
| Persistence | JSON files in `~/.local/share/agents-discussion/runs/` |
| Config | `pydantic-settings` + `.env` |
| Python | 3.11+ |

---

## File structure (src/agents_discussion/)

```
graph.py          — LangGraph graph definition, agent nodes, routing
state.py          — TypedDict DebateState + Pydantic models (Hypothesis, ModeratorDecision…)
web.py            — FastAPI routes, SSE streaming, RunStore, RunSession
models.py         — ChatOpenAI factory for GitHub Models / Copilot
pricing.py        — Price table and per-token cost estimation (USD/1M)
prompts.py        — Per-agent prompt construction (pure functions)
prompt_store.py   — Loading of YAML templates (built-in + custom)
config.py         — Pydantic-settings, env vars with aliases
tools.py          — @tool: SSH, kubectl, HTTP, Prometheus, Loki, Elasticsearch, DB EXPLAIN, git
runtime.py        — RunControl: cancellation, tool approval, HITL
report.py         — Markdown report generation from the run record
audit.py          — append-only audit.jsonl per tool invocation
cli.py            — CLI with argparse + Rich
static/           — Frontend: index.html, css/app.css, js/app.js
prompt_templates/ — built-in YAML (default, performance, errors, data, security)
```

---

## Graph topology (LangGraph)

```
START → diagnostic_agent → skeptic_agent → diagnostic_rebuttal_agent
      → moderator_agent → summarize_history → [user_input_gate] → (loop)
                                                                 → finalize → END
```

- **diagnostic_agent**: primary hypothesis + ReAct tools (model: `diagnostic_model`)
- **skeptic_agent**: falsifies hypotheses + ReAct tools (model: `skeptic_model`)
- **diagnostic_rebuttal_agent**: responds to the skeptic + refines hypotheses (reuses `diagnostic_model`)
- **moderator_agent**: decides whether to continue/close + `flow_directive` for the next round (model: `moderator_model`)
- **summarize_history**: compresses old history when rounds > 2 (model: `summary_model`, defaults to = `moderator_model`)
- **user_input_gate**: optionally blocks between rounds for operator comments

### Routing

- `moderator_agent` → `finalize` if: status ∉ {continue, needs_more_data} OR round > max_rounds
- `moderator_agent` → `summarize_history` if: it continues
- `summarize_history` → `user_input_gate` if: `pause_between_rounds=True`
- `summarize_history` → `diagnostic_agent` otherwise

---

## Main state (DebateState)

Key fields of the TypedDict (see `state.py` for the full definition):

```python
topic, context          # Input problem and context
round, max_rounds       # Current round counter and maximum
diagnostic_model        # Name of the model assigned to diagnosis
skeptic_model           # Skeptic's model
moderator_model         # Moderator's model
summary_model           # Model used to compress history (empty = uses moderator_model)
history                 # List[DebateMessage] with Annotated reducer (append)
token_usage             # Dict[agent_node, {input_tokens, output_tokens, total_tokens}] — reducer _merge_usage
hypotheses              # List[Hypothesis] with merge-by-id reducer (_merge_hypotheses)
round_log               # List[DebateRound] with append reducer
tool_calls_log          # List[ToolCallEntry] with append reducer
moderator_decision      # ModeratorDecision | None (moderator's latest verdict)
early_out_recommended   # bool — signal from the diagnostic agent
run_id                  # str — links nodes with RunControl for approval/cancellation
compress_history        # bool — enables history compression
```

---

## Active configuration (`.env`)

```env
DIAGNOSTIC_MODEL=copilot/gpt-4o
SKEPTIC_MODEL=copilot/claude-sonnet-4.6
MODERATOR_MODEL=copilot/claude-sonnet-4.6
MAX_ROUNDS=10
MAX_TOOL_CALLS_PER_AGENT=50
CONFIDENCE_THRESHOLD=0.8
GITHUB_MODELS_BASE_URL=https://models.github.ai/inference
```

Authentication tokens (GITHUB_TOKEN / COPILOT_TOKEN) are configured separately, not documented here.

### Additional relevant variables

```env
SUMMARY_MODEL=            # Empty → uses MODERATOR_MODEL
COMPRESS_HISTORY=true
EARLY_OUT_THRESHOLD=0.9
TOOL_APPROVAL_REQUIRED=true
APPROVAL_REQUIRED_TOOLS=run_ssh_command,run_local_command,run_kubectl,run_db_explain
MODEL_PRICES_FILE=        # Optional: JSON with USD/1M prices for cost estimation
DATA_DIR=~/.local/share/agents-discussion/runs
```

---

## Persistence flow

1. `POST /api/runs` → creates `meta` with `run_id`, `timestamp` (= start), models
2. `RunStore.create_stub()` → writes a JSON stub in `DATA_DIR/{run_id}.json` with `status: "running"`
3. `asyncio.to_thread(_run_debate_sync)` → worker thread runs the graph
4. `_run_debate_sync` iterates over `stream_debate_events()` → publishes each event to `RunSession.events`
5. On completion in `_drive_run.finally`: computes `finished_at`, `duration_seconds`, extracts `token_totals`/`cost_estimate`
6. `RunStore.save()` → writes the final JSON atomically (temp + rename)

Fields in the persisted JSON:
```json
{
  "run_id", "topic", "timestamp", "finished_at", "duration_seconds",
  "status", "models", "reasoning_effort", "template", "language",
  "parent_run_id", "token_totals", "cost_estimate", "context", "events"
}
```

`_EPHEMERAL_EVENTS` (not persisted): `agent_turn_started`, `agent_delta`

---

## Token capture

Implemented in `graph.py`. Each LLM call returns `usage_metadata`:

- **Agents with tools** (`_run_with_tools`): accumulates `usage_metadata` per ReAct loop iteration;
  returns `(content, tool_log, usage_dict)`. The nodes add `{"token_usage": {node_name: usage_dict}}` to the state.
- **Streaming** (`_invoke_streaming`): `stream_usage=True` in `model.stream()`.
- **Moderator**: `with_structured_output(ModeratorDecision, include_raw=True)` to capture the tokens of the structured output.
- **Summary**: directly from `summary_response.usage_metadata`.
- `stream_debate_events()` accumulates from the graph updates and emits in `run_finished`:
  `{"type": "run_finished", "token_totals": {...}, "cost_estimate": {...}}`

### Cost estimation (`pricing.py`)

- Default price table for ~30 models (OpenAI, Anthropic, Google, Meta, Mistral, Phi, Cohere)
- `_normalize_name(model)`: strips prefixes (`copilot/`, `openai/`…), normalizes separators to `-`
- `_find_price(model, prices)`: exact lookup + fuzzy match by longest substring
- `estimate_cost(token_usage, models_by_role, prices_file)` → `{by_node, total_usd, has_prices}`
- Customizable via `MODEL_PRICES_FILE` (.env) → JSON `{"model": {"input": X, "output": Y}}`

---

## Code conventions

- **Formatter/Linter**: Ruff (target-version = py311, line-length = 120)
- **Mandatory pre-commit**: `ruff check .` + `ruff format --check .` + `python -m py_compile *.py` (also enforced by CI — `.github/workflows/ci.yml`)
- **Versioning**: SemVer driven by Conventional Commits; single source of truth `__version__` in `__init__.py` (see [Versioning & releases](#versioning--releases))
- **Imports**: stdlib → third-party → first-party (absolute, not relative)
- **Type hints**: required on all signatures; `str | None` (not `Optional[str]`)
- **Logging**: `_log = logging.getLogger(__name__)`, no `print()`
- **LLM errors**: `except Exception as exc:  # noqa: BLE001` + `_log.warning`
- **Node returns**: partial dicts (only the modified fields)
- **Frontend JS**: `const`/`let`, no `var`; always `esc()` for user text, `md()` for markdown
- **New state fields**: add them in `state.py` with the appropriate reducer; initialize in `create_initial_state()`
- **Tests**: `tests/` with pytest (`.venv/bin/python -m pytest`); pure logic without LLM (stub models, monkeypatch of `create_github_model`/`get_settings`)

---

## Versioning & releases

- **Scheme**: Semantic Versioning (`MAJOR.MINOR.PATCH`) driven by Conventional Commits
  (`feat` → minor; `fix`/`perf`/`refactor` → patch; `!` / `BREAKING CHANGE` → major;
  `docs`/`style`/`chore`/`ci` → no release on their own).
- **Single source of truth**: `__version__` in `src/agents_discussion/__init__.py`;
  `pyproject.toml` derives it via `[tool.hatch.version]`. Never edit it in two places.
- **Cut a release**: bump `__version__` → commit `chore(release): vX.Y.Z` → `git tag vX.Y.Z`
  → push the tag. The tag push triggers `.github/workflows/release.yml`, which checks the
  tag matches `__version__`, builds the sdist + wheel, and publishes a GitHub Release with
  auto-generated notes.
- **CI** (`.github/workflows/ci.yml`): runs `ruff check`, `ruff format --check` and `pytest`
  on push to `main` and on PRs (ruff pinned to 0.15.18 for reproducible formatting).

---

## Important patterns

### Adding a new state field

1. Add it to `DebateState` in `state.py` with its reducer if it is accumulable
2. Initialize it in `create_initial_state()` in `graph.py`
3. If it must be persisted in the history list: add it to the `list_runs` `keys` in `web.py:89`
4. If it must be in the final record: include it in `RunSession.record()` or in the extra registration

### Adding a new tool

1. Define it in `tools.py` with `@tool`
2. Add it to `APPROVAL_REQUIRED_TOOLS` if it is sensitive
3. The agents see it automatically if `tools_enabled=True` and the name is in `enabled_tool_set`

### Adding a new SSE event type

1. Emit it from `stream_debate_events()` (graph.py) or `RunControl.emit()` (runtime.py)
2. Handle it in `renderEvent(ev)` in app.js
3. If it is not ephemeral, it is persisted automatically in the run's JSON

### Agent skips

The `skeptic_agent` and `diagnostic_rebuttal_agent` nodes check `_should_skip()` at the start.
Skips are implemented as passthrough (they return a placeholder), not as a graph rebuild.

---

## Run JSON structure (relevant events)

```
run_started         → {topic, max_rounds, confidence_threshold, template, language}
agent_turn_started  → {agent_node, agent_role}                    [EPHEMERAL]
agent_delta         → {agent_node, agent_role, delta}             [EPHEMERAL]
agent_completed     → {node, role, content}
agent_reasoning     → {agent_node, agent_role, content}
agent_skipped       → {node, role, rationale}
tool_call_started   → {call_id, agent_node, tool_name, args}
tool_call           → {call_id, tool_name, args, result, error, approval, cached, duration_ms}
                      approval ∈ auto|approved|rejected|timeout|cached (cached = served from the ToolCache, no re-execution)
tool_approval_request → {tool_name, args, agent_role}
tool_approval_resolved → {approved, approval}
moderator_decision  → {node, decision: ModeratorDecision, round}
hypothesis_update   → {node, round, hypotheses: [Hypothesis…]}  (full deduplicated snapshot)
history_compressed  → {round, summary}
awaiting_user_input → {round}
user_comment        → {content}
final_result        → {node, content}
run_finished        → {token_totals, cost_estimate}
run_cancelled       → {}
error               → {message}
```

---

## REST API

```
POST   /api/runs                    Start a debate (multipart/form-data)
GET    /api/runs                    List runs (projection without context/events)
GET    /api/runs/{id}               Full run with events
GET    /api/runs/{id}/events        Live SSE stream
DELETE /api/runs/{id}               Delete
POST   /api/runs/{id}/resume        Resume with new evidence
POST   /api/runs/{id}/cancel        Cancel
POST   /api/runs/{id}/approval      Resolve tool approval
POST   /api/runs/{id}/comment       HITL comment between rounds
GET    /api/models                  Available models (catalog)
GET    /api/prompts                 Available templates
GET    /api/settings                Current configuration
GET    /api/runs/{id}/report        Markdown report
```

---

## Recently implemented changes

### Debate history: timing and duration
- `RunSession` records `started_at` (= `meta["timestamp"]`), `finished_at`, `duration_seconds`
- History table: new **Duration** column + `~Xk tok` subtitle when data is available
- Run detail: `.run-timing-bar` bar with Start / End / Duration below the replay banner
- Markdown report: **Start**, **End**, **Duration** lines in the header

### Models-used bug (2 → 3)
- `app.js` `renderHistoryList`: the array now includes `diagnostic + skeptic + moderator` (moderator was missing before)

### Token consumption + cost estimation
- New `pricing.py` module with a price table and `estimate_cost()`
- `DebateState.token_usage` with the `_merge_usage` reducer that accumulates per node
- Capture at all points: streaming (`stream_usage=True`), moderator (`include_raw=True`), summary (`.usage_metadata`)
- `run_finished` event includes `token_totals` and `cost_estimate`
- Frontend: `buildTokenStatsCard()` in the run detail
- Markdown report: **Token consumption** section with a per-agent table and estimated cost
- `.env`: new `MODEL_PRICES_FILE` variable documented

## Hypothesis model (real implementation)

The hypothesis map is integrated into the real UI (the **Map** tab).

### `Hypothesis` model (`state.py`)

```python
class HypothesisTransition(BaseModel):    # ALWAYS serialize with model_dump(by_alias=True)
    round: int
    from_state: str | None   # alias "from"; None = creation
    to_state: str             # alias "to"
    agent: str
    note: str

class Hypothesis(BaseModel):
    id: str                # raw id from the LLM (HYPOTHESIS-<n>); canonical merge key
    text: str
    state: Literal["active", "rejected", "confirmed"]
    proposer: str          # always "diagnostic_agent" in practice
    round: int             # CREATION round (transitions carry their own round)
    probability: float | None  # P estimated by the agents (format "### HYPOTHESIS-n [P=0.6]"); clamped during extraction
    supporting_evidence: list[str]   # [tool:...] lines from the hypothesis block
    rejected_reason: str | None
    transitions: list[HypothesisTransition]
```

The skeptic recalibrates P per hypothesis (`[P=<0-1>]` next to the id in its response, parsed in
`skeptic_agent`); during the merge, an incoming P of `None` keeps the previous one.

### Implemented design

- **`_merge_hypotheses` reducer** (state.py): merge by id, ordered by first appearance; keeps the original `round`/`proposer`, takes the incoming `state`/`text`/`rejected_reason`, unions the evidence, dedups transitions by `(round, to_state, agent)` discarding re-emitted creations.
- **Stable IDs**: the diagnostic prompt receives the hypotheses under debate (`diagnostic_prompt(..., hypotheses=...)`) and instructs reusing exact ids and continuing the numbering. The unformatted fallback uses a round-scoped id `R{n}-F1` to avoid merging fallbacks from different rounds.
- **Evidence**: `_split_hypothesis_block` (graph.py) separates the hypothesis text from the `[tool:...]` lines (max 5, 250 chars each).
- **Transitions**: creation in `_extract_hypotheses`; state changes in `skeptic_agent` (which now returns **only** the modified hypotheses; the reducer merges them).
- **`hypothesis_update` SSE event**: emitted in `stream_debate_events()` after the `agent_completed` of any node whose update carries `hypotheses`; payload = full deduplicated snapshot (`model_dump(by_alias=True)`, rebuilt with the reducer itself because the stream updates are partial). It is not ephemeral → it is persisted and the replay rebuilds the map.

### Deliberate exclusion

`diagnostic_rebuttal_agent` does not update hypotheses: its output is free text with no parseable format. The SSE emission is generic (any `node_update` with `hypotheses`), so incorporating it only requires that the node return hypotheses.

### Frontend: Map tab

- **`static/js/hypothesis-map.js`** — IIFE module that exposes `window.HypoMap` with `setTopic / update / setDecision / reset / show`.
- Deterministic `concentric` layout: topic at the center, ring = creation round. Incremental render (`cy.add()` / `node.data()`, without destroy+rebuild); `fit` only on the first layout so as not to steal the user's zoom.
- Mandatory lazy-init: the container starts hidden (Cytoscape would measure 0×0); `show()` creates/resizes and syncs if there are pending updates (dirty flag).
- Dynamic round filter (`.dimmed` class), side detail panel (evidence, rejection reason, transition history), tooltip with full text, replay of state transitions (not just appearances).
- Leading hypothesis: token Jaccard between `moderator_decision.leading_hypothesis` and `h.text`; a score ≥ 0.3 marks the node with a `.leader` halo; the top banner with the moderator's text is always shown.
- Wiring in `app.js`: `hypothesis_update` branch in `renderEvent` (early-return, before `closeToolGroup`), `setTopic` on `run_started`, `setDecision` on `moderator_decision`, `reset` on `clearThread()`, `show` when activating the tab.

---

## Diagnostic capability and token economy

### Methodological prompting (v2 templates)

The 10 YAML files (`prompt_templates/{default,performance,errors,data,security}.{es,en}.yaml`, `version: 2`)
share a common block marked with `# --- Metodología v1 ... ---` (replicated, NOT composed:
the files are self-contained so as not to break custom overrides; keep them synchronized by hand):

- `diagnostic_system` → "Methodology" block: chronology first (git_recent_changes + correlation),
  differential diagnosis (≥2 alternatives), information/cost prioritization, tool→signal guide.
  In `performance` the chronology bullet is omitted (its "Focus" already covers it).
- `skeptic_system` → discriminating test between competing hypotheses + do not re-open rejected ones without new evidence.
- `moderator_system` → interpretive confidence scale (<0.3 conjecture / 0.3-0.6 plausible /
  0.6-0.8 probable / >0.8 confirmed) + use the agents' P values as input.

### ToolCache (runtime.py)

Per-run cache of tool results, shared between agents: `RunControl.tool_cache`
(CLI without control → ephemeral local cache per loop). Hit only if **same round** and age ≤ 300 s;
only executions with `error=False` and approval auto/approved are cached. A hit does not re-execute nor
re-request approval; it is logged with `approval="cached"` (audit + tool_calls_log + `tool_call` event
with `cached: true`, "cache" badge in the UI). The served result carries the prefix
`[cached: already executed by <role> in round <n>]` visible to the LLM.

### Reducing redundancy in prompts

- `_history_before_current_round` (graph.py): skeptic/rebuttal/moderator receive the history ONLY
  up to the previous round (+ `role=="user"` comments of the current one) — their responses for the
  current round already go explicitly in the prompt. The diagnosis receives the full history.
  NOTE: the rebuttal's role in history is `"diagnostic_rebuttal"` (without `_agent`).
- **Compression from round 2** (`_history_mode`): compressed if there is a summary and round ≥ 2. In compressed
  mode the "history" passed to the prompts is only the tail after the last moderator
  (HITL comments); `format_history` renders the summary + that tail.
- `summarize_history`: the finished round is derived from the history (`finished_round = number of moderator
  messages`), NOT from `state["round"]` (the moderator already incremented it with `continue` — a historical bug
  that prevented the summary from being generated). The summary is **cumulative**: it integrates the previous summary with the new
  messages in ≤400 words.
- The three `*_deliver` (prompts.py) ask for ~600 words maximum and to cite tools in summary form.
- `_truncate` (tools.py) keeps 2/3 head + 1/3 tail of the output (it used to cut only the beginning).

---

## Project reference documentation

| Document | Content |
|---|---|
| `docs/architecture.md` | Detailed design, state schema, topology, decisions |
| `docs/coding-style.md` | Python and JS conventions, LangGraph patterns |
| `docs/operations.md` | Deployment, full configuration, troubleshooting |
| `docs/usage.md` | CLI, web UI, API, templates, tools |
| `docs/reference/` | Generated from code (API, CLI, config, tools, events) — see `scripts/gen_docs.py` |
