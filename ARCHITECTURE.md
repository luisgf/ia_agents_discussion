# Architecture

This document describes the system architecture of **Agents Discussion**, a multi-agent technical diagnosis platform built on LangGraph.

---

## Table of Contents

- [Overview](#overview)
- [System Boundaries](#system-boundaries)
- [Component Model](#component-model)
- [Data Flow](#data-flow)
- [State Machine](#state-machine)
- [Debate Graph Topology](#debate-graph-topology)
- [Agent Nodes](#agent-nodes)
- [Structured Hypotheses](#structured-hypotheses)
- [History Compression](#history-compression)
- [Adaptive Flow Control](#adaptive-flow-control)
- [Early-Out Mechanism](#early-out-mechanism)
- [Tool Execution Layer](#tool-execution-layer)
- [Event Streaming](#event-streaming)
- [Persistence Model](#persistence-model)
- [Extension Points](#extension-points)

---

## Overview

Agents Discussion orchestrates a structured debate between three LLM agents to diagnose technical problems. The system is designed around a **directed state graph** (LangGraph's `StateGraph`) where each node represents an agent turn or a control gate. The graph executes synchronously within a worker thread, while the web layer communicates via event streams.

Key architectural decisions:

1. **LangGraph for orchestration** — Provides checkpointing, conditional routing, and typed state transitions.
2. **Synchronous graph, async web** — The graph runs in a blocking thread; the web layer uses asyncio with SSE for real-time communication.
3. **ReAct tool loop per agent** — Each agent can invoke tools repeatedly within its turn.
4. **Human-in-the-loop gating** — Tool approvals and round pauses block the graph via threading primitives.
5. **Structured state over text** — Hypotheses, flow directives, and round logs are first-class state objects, not free-form text.

---

## System Boundaries

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Agents Discussion System                          │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │   CLI Entry  │    │  Web Server  │    │   Programmatic│                  │
│  │   (cli.py)   │    │  (web.py)    │    │   (graph.py)  │                  │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘                  │
│         │                   │                   │                           │
│         └───────────────────┼───────────────────┘                           │
│                             ▼                                               │
│                  ┌──────────────────────┐                                   │
│                  │   Debate State Graph │                                   │
│                  │     (graph.py)       │                                   │
│                  │  ┌────────────────┐  │                                   │
│                  │  │  State Graph   │  │                                   │
│                  │  │ ┌────────────┐ │  │                                   │
│                  │  │ │Agent Nodes │ │  │                                   │
│                  │  │ │  + Tools   │ │  │                                   │
│                  │  │ └────────────┘ │  │                                   │
│                  │  └────────────────┘  │                                   │
│                  └──────────────────────┘                                   │
│                             │                                               │
│              ┌──────────────┼──────────────┐                                │
│              ▼              ▼              ▼                                │
│       ┌───────────┐  ┌───────────┐  ┌───────────┐                         │
│       │ GitHub    │  │ Tools     │  │ RunStore  │                         │
│       │ Models /  │  │ (SSH,     │  │ (JSON     │                         │
│       │ Copilot   │  │ kubectl,  │  │  files)   │                         │
│       │           │  │ DB, etc.) │  │           │                         │
│       └───────────┘  └───────────┘  └───────────┘                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Model

### Core Components

| Component | File | Responsibility |
|---|---|---|
| **CLI** | `cli.py` | Argument parsing, context assembly, console output via Rich |
| **Web Server** | `web.py` | FastAPI routes, SSE streaming, run lifecycle management |
| **State Graph** | `graph.py` | LangGraph definition, agent nodes, routing logic |
| **State Models** | `state.py` | TypedDict `DebateState`, Pydantic models for decisions and hypotheses |
| **Prompts** | `prompts.py` | Per-turn user messages, history formatting, JSON schemas |
| **Prompt Store** | `prompt_store.py` | Template loading (built-in + custom YAML overrides) |
| **Models** | `models.py` | GitHub Models / Copilot model factory with reasoning effort gating |
| **Tools** | `tools.py` | ReAct tools: SSH, kubectl, HTTP, Prometheus, Loki, Elasticsearch, DB EXPLAIN, git |
| **Runtime** | `runtime.py` | `RunControl`: cancellation, approval gating, HITL comments |
| **Config** | `config.py` | Pydantic-settings with env var aliases |
| **Audit** | `audit.py` | Append-only `audit.jsonl` for every tool invocation |
| **Reports** | `report.py` | Markdown generation from stored run records |

### Data Components

| Component | File | Responsibility |
|---|---|---|
| **Context Files** | `context_files.py` | File reading with optional secret redaction |
| **Project Context** | `project_context.py` | Directory globbing for `--project` |
| **Auth Copilot** | `auth_copilot.py` | OAuth device flow for GitHub Copilot tokens |

---

## Data Flow

### Typical Debate Flow (Web)

```
┌─────────┐     POST /api/runs      ┌──────────┐
│  User   │ ──────────────────────> │  FastAPI │
│ Browser │                         │  (web)   │
└─────────┘                         └────┬─────┘
                                         │
                    ┌────────────────────┘
                    │ create RunSession
                    │ register RunControl
                    ▼
           ┌─────────────────┐
           │  RunSession     │
           │  (in-memory)    │
           └────────┬────────┘
                    │ asyncio.to_thread()
                    ▼
           ┌─────────────────┐
           │ Worker Thread   │
           │ _run_debate_sync│
           └────────┬────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │ stream │ │  LLM   │ │ tools  │
   │ events │ │ calls  │ │ exec   │
   └───┬────┘ └───┬────┘ └───┬────┘
       │          │          │
       ▼          ▼          ▼
   ┌────────────────────────────────┐
   │      RunControl.emit()         │
   │  (thread-safe via lock)        │
   └───────────────┬────────────────┘
                   │
                   ▼
          ┌───────────────┐
          │  SSE Stream   │
          │  (browser)    │
          └───────────────┘
```

### CLI Flow

CLI bypasses the web layer. `run_debate()` is called directly, which invokes `graph.invoke()`. Tool calls execute immediately without approval gating (since `RunControl` is `None`).

---

## State Machine

The `DebateState` is a `TypedDict` that LangGraph passes between nodes. It is **immutable in intent** — nodes return partial updates, and LangGraph merges them via annotated reducers.

### State Schema

```python
class DebateState(TypedDict):
    # Identity & context
    topic: str
    context: str
    round: int
    max_rounds: int
    confidence_threshold: float
    early_out_threshold: float

    # Per-turn outputs (written by agent nodes)
    diagnostic_response: str
    skeptic_response: str
    diagnostic_rebuttal: str
    moderator_decision: ModeratorDecision | None

    # History & persistence
    history: Annotated[list[DebateMessage], append_messages]
    history_summary: str
    round_log: Annotated[list[DebateRound], _append_list]
    tool_calls_log: Annotated[list[ToolCallEntry], _append_list]
    final_result: str | None

    # Structured hypotheses
    hypotheses: Annotated[list[Hypothesis], _append_list]

    # Early-out signal
    early_out_recommended: bool
    early_out_confidence: float
    early_out_rationale: str

    # Model configuration
    diagnostic_model: str
    skeptic_model: str
    moderator_model: str
    summary_model: str

    # Reasoning effort
    diagnostic_reasoning_effort: str
    skeptic_reasoning_effort: str
    moderator_reasoning_effort: str

    # Misc
    run_id: str
    template: str
    language: str
    compress_history: bool
```

### Reducers

- `history`, `tool_calls_log`, `round_log`, `hypotheses` — use `_append_list`, which concatenates new items to the existing list (or initializes from `None`).
- All other keys — overwritten by the node's return dict (standard LangGraph behavior).

---

## Debate Graph Topology

```
                    ┌─────────────────┐
         ┌─────────│      START      │◄─────────┐
         │         └────────┬────────┘          │
         │                  │                   │
         │         ┌────────▼────────┐          │
         │         │ diagnostic_agent│          │
         │         └────────┬────────┘          │
         │                  │                   │
         │         ┌────────▼────────┐          │
         │         │  skeptic_agent  │          │ (skipped if
         │         └────────┬────────┘          │  flow_directive
         │                  │                   │  says so)
         │         ┌────────▼────────┐          │
         │         │diagnostic_rebuttal│         │ (skipped if
         │         └────────┬────────┘          │  flow_directive
         │                  │                   │  says so)
         │         ┌────────▼────────┐          │
         │         │ moderator_agent │          │
         │         └────────┬────────┘          │
         │                  │                   │
         │         ┌────────▼────────┐          │
         │    ┌────│ summarize_history│◄───┐    │
         │    │    └─────────────────┘    │    │
         │    │           │               │    │
         │    │    ┌──────┴──────┐        │    │
         │    ▼    ▼             │        │    │
         │ ┌──────────────┐      │        │    │
         │ │ user_input_gate    │      │    │ (if pause_between_rounds)
         │ └──────┬───────┘      │        │    │
         │        │              │        │    │
         │        └──────────────┘        │    │
         │                   │            │    │
         │              continue          │    │
         │              (status=continue) │    │
         │                   │            │    │
         └───────────────────┘            │    │
                                          │    │
                                   finalize    │
                                   │    │
                                   ▼    │
                              ┌────────┐│
                              │   END  │┘
                              └────────┘
```

### Routing Decisions

| From Node | Condition | To Node |
|---|---|---|
| `moderator_agent` | `status not in {continue, needs_more_data}` or `round > max_rounds` | `finalize` |
| `moderator_agent` | `status in {continue, needs_more_data}` | `summarize_history` |
| `summarize_history` | `pause_between_rounds == True` | `user_input_gate` |
| `summarize_history` | otherwise | `diagnostic_agent` |
| `user_input_gate` | always | `diagnostic_agent` |

---

## Agent Nodes

### Diagnostic Agent

- **Model**: `diagnostic_model` (default: GPT-4o)
- **Temperature**: 0.2
- **Role**: Proposes the leading hypothesis, executes experiments, reports tool outputs
- **Prompt constraints**: Must format hypotheses as `### HYPOTHESIS-<id>\nText: ...`
- **Output fields**: `diagnostic_response`, `hypotheses`, `early_out_recommended`, `early_out_confidence`, `early_out_rationale`

### Skeptic Agent

- **Model**: `skeptic_model` (default: Claude Sonnet)
- **Temperature**: 0.1
- **Role**: Falsifies the diagnostic hypothesis, proposes alternatives, evaluates evidence quality
- **Skip condition**: `flow_directive.skip_skeptic == True` from previous moderator
- **Output fields**: `skeptic_response`, updated `hypotheses` states

### Diagnostic Rebuttal Agent

- **Model**: `diagnostic_model` (reuses same model as diagnostic)
- **Temperature**: 0.2
- **Role**: Responds to skeptic critiques, updates hypothesis, refines experiment
- **Skip condition**: `flow_directive.skip_rebuttal == True` from previous moderator
- **Output fields**: `diagnostic_rebuttal`

### Moderator Agent

- **Model**: `moderator_model` (default: Claude Sonnet)
- **Temperature**: 0.0
- **Role**: Reviews full round history and decides status + flow for next round
- **Structured output**: Uses `model.with_structured_output(ModeratorDecision)` with JSON fallback
- **Streaming**: Emits `agent_turn_started` at node start; the structured path cannot stream, but the JSON fallback path streams text/reasoning deltas via `_invoke_streaming`
- **Output fields**: `moderator_decision` (includes embedded `flow_directive`)

---

## Structured Hypotheses

Hypotheses are no longer implicit text. Each hypothesis is a typed object:

```python
class Hypothesis(BaseModel):
    id: str           # e.g. "H-1", "H-2"
    text: str
    state: Literal["active", "rejected", "confirmed"]
    proposer: str     # node name
    round: int
    supporting_evidence: list[str]
    rejected_reason: str | None
```

### Lifecycle

1. **Creation**: `diagnostic_agent` extracts hypotheses from its response via regex (`### HYPOTHESIS-...`).
2. **Review**: `skeptic_agent` evaluates each by ID and the system parses acceptance/rejection markers.
3. **Update**: States are updated in the shared state; the moderator sees the full table.
4. **Reporting**: Final reports include a timeline of hypothesis evolution.

---

## History Compression

To prevent prompt bloat after round 2, the system compresses older history.

### Trigger

- `compress_history == True` (default)
- `round > 2`

### Algorithm

1. After `moderator_agent`, the `summarize_history` node runs.
2. It takes all messages from the **completed round** (`_last_round_messages`).
3. A summary prompt is sent to the `summary_model` (default: same as moderator).
4. The summary is stored in `state["history_summary"]`.

### Prompt Impact

Rounds 1–2: Full history is passed to every agent.
Rounds 3+: Agents receive:

```
Summary of previous rounds:
<compressed summary>

Last round in full:
[diagnostic_agent] ...
[skeptic_agent] ...
[diagnostic_rebuttal] ...
[moderator] ...
```

---

## Adaptive Flow Control

The moderator can request non-standard flows via `flow_directive`:

```python
class FlowDirective(TypedDict):
    skip_skeptic: bool
    skip_rebuttal: bool
    rationale: str
```

### When Used

- **Early-out**: Diagnostic signals conclusive evidence → moderator skips skeptic.
- **Empty critique**: Skeptic adds nothing new → moderator skips rebuttal.
- **Expedited diagnosis**: Human operator provides conclusive context → moderator short-circuits.

### Implementation

Skips are implemented as **passthrough nodes**, not dynamic graph reconstruction. The graph topology remains static. Skipped nodes return placeholder text without calling the LLM.

---

## Early-Out Mechanism

When the diagnostic agent believes evidence is conclusive, it can append:

```
EARLY_OUT_RECOMMENDED: true
EARLY_OUT_CONFIDENCE: 0.95
EARLY_OUT_RATIONALE: EXPLAIN shows missing index on orders.created_at; sequential scan on 10M rows
```

The moderator sees the `early_out_threshold` (default 0.9) in its prompt and may:
- Set `status = "propose_fix"` if confidence is high enough.
- Set `flow_directive.skip_skeptic = True` to bypass the skeptic.

---

## Tool Execution Layer

Each agent runs inside a **ReAct loop** (`_run_with_tools`):

```
LLM → tool_call proposal → approval check → execution → result → LLM → ... → final text
```

### Approval Gating

For web runs, `RunControl.request_approval()` blocks the thread until:
- The operator approves/rejects via POST `/api/runs/{id}/approval`
- The timeout expires (default 300s)
- The run is cancelled

### Tool Cards

The frontend groups consecutive tool calls into a collapsible **Tool Group** card. Each tool call has a `call_id` shared between `tool_call_started` (spinner) and `tool_call` (result).

---

## Event Streaming

### Event Types

| Event | Emitter | Description |
|---|---|---|
| `run_started` | `stream_debate_events` | Initial metadata |
| `agent_turn_started` | `_invoke_streaming` | Streaming begins |
| `agent_delta` | `_invoke_streaming` | Token chunk |
| `agent_reasoning_delta` | `_invoke_streaming` | Thinking-token chunk (reasoning models) |
| `agent_thinking` | `_invoke_streaming` | Full chain of thought for one LLM iteration (persisted) |
| `agent_reasoning` | `_run_with_tools` | Text before tool calls |
| `agent_completed` | `stream_debate_events` | Final agent text |
| `agent_skipped` | `stream_debate_events` | Node was bypassed |
| `tool_call_started` | `_run_with_tools` | Tool execution begins |
| `tool_call` | `_run_with_tools` | Tool execution complete |
| `tool_approval_request` | `RunControl` | Waiting for operator |
| `tool_approval_resolved` | `RunControl` | Operator responded |
| `moderator_decision` | `stream_debate_events` | Round decision |
| `summary_started` | `summarize_history` | History compression begins (ephemeral) |
| `history_compressed` | `stream_debate_events` | Summary generated |
| `awaiting_user_input` | `RunControl` | HITL pause |
| `user_comment` | `RunControl` | Operator comment inserted |
| `final_result` | `stream_debate_events` | Debate concluded |
| `run_finished` | `stream_debate_events` | Stream ends |
| `run_cancelled` | `RunControl` / worker | Cancelled by operator |
| `error` | worker | Exception caught |

### Thread Safety

`RunControl.emit()` delegates to `RunSession.publish()`, which uses a `threading.Lock()` to append events to a shared list. SSE subscribers poll this list with an index cursor.

---

## Persistence Model

### Run Storage (`RunStore`)

Each run is a single JSON file in `DATA_DIR` (`~/.local/share/agents-discussion/runs/{run_id}.json`).

**Stub** (created at start):
```json
{
  "run_id": "...",
  "topic": "...",
  "timestamp": "...",
  "status": "running",
  "events": []
}
```

**Final record** (atomically written on completion):
- Metadata, events (without ephemeral types), context, and tool logs.

### Audit Log

`audit.jsonl` in `DATA_DIR` contains one JSON line per tool invocation:
```json
{"timestamp": "...", "run_id": "...", "agent": "...", "tool": "...", "args": {}, "result": "...", "error": false, "approval": "approved"}
```

### Orphan Recovery

On startup, `RunStore._mark_orphans()` scans all JSON files and marks `status == "running"` as `"interrupted"`.

---

## Extension Points

### Adding a New Tool

1. Define the tool in `tools.py` using `@tool` decorator.
2. Optionally add to `APPROVAL_REQUIRED_TOOLS` in `.env`.
3. Document signature and constraints.

### Adding a New Template

1. Copy an existing YAML from `src/agents_discussion/prompt_templates/`.
2. Name it `<name>.<lang>.yaml`.
3. Drop it in `PROMPTS_DIR` (default: `~/.local/share/agents-discussion/prompts`).

### Adding a New Node to the Graph

1. Define the node function with signature `fn(state: DebateState) -> dict`.
2. Add it to `build_graph()` with `builder.add_node()`.
3. Add routing in `route_after_moderator()` or create a new `route_after_*()` function.
4. If the node produces text output, add to `AGENT_EVENT_FIELDS` (only if it should emit `agent_completed`).
5. Update `stream_debate_events` to yield its events.

### Custom Model Provider

Extend `models.py`:
1. Add a factory function (e.g., `_create_azure_model`).
2. Update `create_github_model()` router to recognize the new prefix.
3. Handle `reasoning_effort` gating if applicable.

---

## Scaling Considerations

| Bottleneck | Mitigation |
|---|---|
| LLM latency | History compression reduces tokens per call. Summary model can be cheaper/faster. |
| Token cost | Compression converts linear token growth to near-constant. Early-out avoids unnecessary rounds. |
| Concurrent runs | Each run is an independent thread with its own `RunControl`. No shared mutable state between runs. |
| Graph recursion limit | `_graph_config()` sets `recursion_limit = max_rounds * 8 + 10` to accommodate all nodes. |
