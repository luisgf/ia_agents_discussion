# Coding Style

This document defines the coding conventions for the Agents Discussion project.

---

## Table of Contents

- [Language & Tooling](#language--tooling)
- [License Headers](#license-headers)
- [Project Structure](#project-structure)
- [Imports](#imports)
- [Type Hints](#type-hints)
- [Naming Conventions](#naming-conventions)
- [Docstrings](#docstrings)
- [Error Handling](#error-handling)
- [Logging](#logging)
- [Constants & Magic Values](#constants--magic-values)
- [Async & Threading](#async--threading)
- [LangGraph Patterns](#langgraph-patterns)
- [Frontend (JavaScript)](#frontend-javascript)
- [Pre-commit Rules](#pre-commit-rules)

---

## Language & Tooling

- **Python**: 3.11+
- **Formatter**: Ruff (replaces Black + isort)
- **Linter**: Ruff. The project does **not** pin an explicit rule set, so Ruff's default lint rules apply. Keep new code warning-free under `ruff check .`.
- **Type checker**: relies on IDE / editor type checking (mypy optional); Pydantic provides runtime validation.
- **Frontend**: Vanilla JavaScript, no build step. `marked` for Markdown, `DOMPurify` for sanitization.

### Ruff Configuration (actual — see [`pyproject.toml`](pyproject.toml))

```toml
[tool.ruff]
target-version = "py311"
line-length = 120
```

> Only `target-version` and `line-length` are configured — there is **no** explicit `select`/`ignore`, so Ruff applies its default lint set. If you want stricter linting (e.g. `select = ["E", "W", "F", "I", "N", "UP", "B"]`), declare it in `pyproject.toml` first; do not assume rules that are not present there.

---

## License Headers

Every first-party source file (`*.py`, and the project's own `*.js` under `static/js/`)
starts with a two-line SPDX header. Vendored libraries under `static/vendor/` keep
their upstream headers and are left untouched.

```python
# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later
```

```javascript
// Copyright (C) 2025 Luis González Fernández
// SPDX-License-Identifier: GPL-3.0-or-later
```

New files must include this header. The project is licensed under
**GPL-3.0-or-later**; see [`LICENSE`](LICENSE) for the full text.

---

## Project Structure

```
src/agents_discussion/
├── __init__.py
├── graph.py           # LangGraph definition — ONLY graph topology and nodes
├── state.py           # TypedDict + Pydantic models — shared state schema
├── prompts.py         # Prompt building functions — pure, no side effects
├── prompt_store.py    # Template loading from YAML files
├── models.py          # LLM factory — ChatOpenAI configuration
├── tools.py           # @tool decorated functions — diagnostic tools
├── config.py          # Pydantic-settings + env var mapping
├── runtime.py         # RunControl — synchronization primitives
├── web.py             # FastAPI routes + SSE
├── cli.py             # argparse + Rich console UI
├── report.py          # Markdown report generation from stored runs
├── audit.py           # audit.jsonl append
├── auth_copilot.py    # GitHub Copilot OAuth device flow
├── context_files.py   # File reading with secret redaction
├── project_context.py # Directory globbing for --project
└── static/            # HTML, CSS, JS — no build pipeline
    ├── index.html
    ├── css/
    └── js/
        └── app.js
```

**Rule**: Domain logic belongs in the graph module. Web concerns belong in `web.py`. Never import FastAPI or HTTP concepts into `graph.py`.

---

## Imports

### Order

1. Standard library (`import json`, `from typing import ...`)
2. Third-party (`from langgraph.graph import ...`, `from pydantic import ...`)
3. First-party (`from agents_discussion.state import ...`)

### Style

```python
# ✅ Preferred — explicit imports
from typing import Annotated, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agents_discussion.state import DebateState

# ✅ Acceptable for many imports
from agents_discussion.prompts import (
    diagnostic_prompt,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)

# ❌ Avoid wildcard or deeply nested relative imports
from .state import *  # NO
from ..utils.helpers import thing  # NO — use absolute
```

### Forward References

Use `from __future__ import annotations` when needed for forward reference resolution. This is present in files that define mutually recursive types.

---

## Type Hints

### Required

All function signatures must have type hints for parameters and return types.

```python
# ✅
def _extract_hypotheses(text: str, proposer: str, round_number: int) -> list[Hypothesis]:
    ...

# ❌
def _extract_hypotheses(text, proposer, round_number):
    ...
```

### TypedDict

Use `TypedDict` for LangGraph state to get structural typing + readonly semantics.

```python
class DebateState(TypedDict):
    topic: str
    round: int
    history: Annotated[list[DebateMessage], append_messages]
```

### Pydantic Models

Use `BaseModel` for structured outputs from LLMs and for data validation at boundaries.

```python
class ModeratorDecision(BaseModel):
    status: Literal["continue", "final_diagnosis", "needs_more_data", "propose_fix", "structured_uncertainty"]
    confidence: float = Field(ge=0.0, le=1.0)
```

### None Safety

```python
# ✅ Use | None (Python 3.10+ syntax)
value: str | None = None

# ❌ Avoid Optional[str] in new code
```

---

## Naming Conventions

| Entity | Convention | Example |
|---|---|---|
| Modules | `snake_case.py` | `graph.py`, `prompt_store.py` |
| Classes | `PascalCase` | `DebateState`, `ModeratorDecision` |
| Functions | `snake_case` | `build_graph()`, `run_debate()` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_OUT`, `_CONTINUE_STATUSES` |
| Private helpers | `_leading_underscore` | `_message_content()`, `_template_for()` |
| LangGraph nodes | `snake_case_agent` | `diagnostic_agent`, `moderator_agent` |
| TypedDict keys | `snake_case` | `diagnostic_response`, `tool_calls_log` |

### Special Prefixes

- `_` (single underscore): Module-private helper.
- `__` (double underscore): Rarely used; prefer module-level privacy over name mangling.

---

## Docstrings

### Style

Use Google-style docstrings for consistency.

```python
def _run_with_tools(
    model_factory: Callable[[], BaseChatModel],
    agent_node: str,
    system_prompt: str,
    user_message: str,
    run_id: str = "",
) -> tuple[str, list[ToolCallEntry]]:
    """Run a model in a ReAct loop: LLM → tool call(s) → LLM → … → final text.

    Returns the agent's final text response and a list of ToolCallEntry records.
    Tools are only used when TOOLS_ENABLED=true in settings.

    Args:
        model_factory: Callable that returns a fresh model instance.
        agent_node: Node name for logging and event emission.
        system_prompt: System message content.
        user_message: User message content (the prompt).
        run_id: Optional run ID for RunControl lookup.

    Returns:
        Tuple of (final_content, tool_invocation_log).
    """
```

### Rules

- All public functions must have docstrings.
- Private helpers should have docstrings if the logic is non-obvious.
- Keep docstrings under 120 characters per line.

---

## Error Handling

### Philosophy

**Fail fast at boundaries, recover gracefully in loops.**

### LLM Call Failures

```python
# ✅ Retry only for transient errors; let structural errors propagate
try:
    result = model.invoke(messages)
except Exception as exc:  # noqa: BLE001
    _log.warning("Model call failed (%s), attempting fallback", exc)
    result = fallback_model.invoke(messages)
```

### Tool Execution

```python
# ✅ Always catch tool errors to prevent the ReAct loop from crashing
try:
    return str(tool_fn.invoke(tool_args)), False
except Exception as exc:  # noqa: BLE001
    return f"Tool error: {exc}", True
```

### Graceful Degradation

```python
# ✅ If a feature fails, fall back to the safe path
try:
    structured = model.with_structured_output(ModeratorDecision)
    decision = structured.invoke(messages)
except Exception:
    _log.warning("Structured output failed, falling back to plain text")
    decision = _parse_moderator_response(model.invoke(messages).content)
```

### Exception Naming

```python
# ✅ Use standard exceptions; custom only when catchable
class RunCancelled(Exception):
    """Raised inside the debate when the operator cancels the run."""
```

---

## Logging

### Logger Pattern

```python
import logging

_log = logging.getLogger(__name__)
```

### Levels

| Level | When to use |
|---|---|
| `DEBUG` | Token counts, prompt sizes, internal routing decisions |
| `INFO` | Run start/finish, round transitions, tool approvals |
| `WARNING` | Model fallback, tool errors, skipped nodes, compression failures |
| `ERROR` | Unhandled exceptions, persistent model failures, graph crashes |

### Format

```python
# ✅ Structured messages with context
_log.warning("moderator structured output failed (%s), falling back", exc)

# ❌ Avoid f-strings in logging calls (lazy evaluation)
_log.warning(f"Failed with {exc}")  # Wastes cycles if level is filtered
```

---

## Constants & Magic Values

### Inline Constants

```python
# ✅ Module-level constants with explanation
_MAX_OUT = 4_000  # Maximum characters kept from any tool output
_CONTINUE_STATUSES = {"continue", "needs_more_data"}

_MODELS_TTL = 300.0  # 5 minutes cache for model catalog
```

### Configuration Defaults

All user-visible defaults live in `config.py` via Pydantic settings, never hardcoded in business logic.

```python
# ✅ In config.py
max_rounds: int = Field(4, alias="MAX_ROUNDS", ge=1, le=10)

# ❌ In graph.py
default_rounds = 4  # NO — read from settings
```

---

## Async & Threading

### Rule: Graph is sync, web is async

```python
# ✅ web.py: async route delegates to thread
async def _drive_run(session: RunSession) -> None:
    await asyncio.to_thread(_run_debate_sync, session)

# ✅ graph.py: blocking I/O (LLM calls) in worker thread
for chunk in model.stream(messages):
    ...
```

### Thread Safety

```python
# ✅ Lock-protected shared state
class RunSession:
    def __init__(self):
        self._lock = threading.Lock()

    def publish(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)
```

### Never use `asyncio` inside graph nodes

LangGraph nodes must not contain `await` or `asyncio` calls. If an LLM provider offers only async APIs, wrap with `asyncio.run()` at the integration layer in `models.py`.

---

## LangGraph Patterns

### Node Return Values

Nodes return **partial dictionaries** that are merged into state.

```python
# ✅ Only return fields you modify
def diagnostic_agent(state: DebateState) -> dict[str, object]:
    return {
        "diagnostic_response": content,
        "history": [DebateMessage(role="diagnostic_agent", content=content)],
        "tool_calls_log": tool_log,
        "hypotheses": hypotheses,
    }
```

### Conditional Edges

```python
# ✅ Explicit mapping of return values to target nodes
builder.add_conditional_edges(
    "moderator_agent",
    route_after_moderator,
    {
        "summarize_history": "summarize_history",
        "finalize": "finalize",
    },
)
```

### State Reducers

Use `Annotated` with custom reducers for list fields that append rather than overwrite.

```python
def _append_list(current: list | None, new: list | None) -> list:
    return (current or []) + (new or [])

class DebateState(TypedDict):
    history: Annotated[list[DebateMessage], append_messages]
    tool_calls_log: Annotated[list[ToolCallEntry], _append_list]
```

### Graph Configuration

Always set `recursion_limit` based on `max_rounds` to prevent LangGraph's default (25) from truncating long debates.

```python
def _graph_config(state: DebateState) -> dict:
    return {"recursion_limit": state["max_rounds"] * 8 + 10}
```

---

## Frontend (JavaScript)

### Style

- No build system; vanilla JavaScript in `static/js/app.js`.
- Use `const` and `let`; never `var`.
- Functions are declarations, not arrow functions for top-level definitions.

### Event Handling

```javascript
// ✅ Guard unknown events gracefully
if (ev.type === 'agent_skipped') {
    closeToolGroup();
    push(buildSkippedCard(ev));
    return;
}

// ✅ Handle missing fields defensively
const content = ev.content || '';
```

### HTML Injection

Always sanitize user-facing content. Use `esc()` for plain text, `md()` for markdown, and `DOMPurify.sanitize()` for parsed HTML.

```javascript
// ✅ Escape everything that could contain user input
card.innerHTML = '<span>' + esc(ev.tool_name) + '</span>';

// ✅ Markdown is parsed then sanitized
const html = md(agentContent);
body.innerHTML = DOMPurify.sanitize(html);
```

---

## Pre-commit Rules

These checks are **enforced by convention, not automation**: the repository has no
`.pre-commit-config.yaml` and no CI workflows, so run them locally before every commit.

### Run Before Commit

1. `ruff check .` — no lint errors
2. `ruff format --check .` — no formatting violations
3. `python -m py_compile src/agents_discussion/*.py` — syntax valid
4. `.venv/bin/python -m pytest` — pure-logic test suite stays green

### Forbidden Patterns

| Pattern | Why |
|---|---|
| `print()` statements | Use `_log` instead |
| Bare `except:` | Always catch `Exception`, never swallow `KeyboardInterrupt` silently |
| `eval()` or `exec()` | Security risk; never process model output with eval |
| `subprocess.run(shell=True)` unless escaped | Use argv form where possible |
| Hardcoded secrets in source | Read from env or `.env` file via `config.py` |
| `time.sleep()` in graph nodes | Prevents cancellation responsiveness |

---

## Testing Conventions

### Unit Tests

```python
# ✅ Test pure helpers without LLM calls
def test_last_round_messages():
    msgs = [
        DebateMessage(role="moderator", content="m1"),
        DebateMessage(role="diagnostic_agent", content="d1"),
    ]
    result = _last_round_messages(msgs, 2)
    assert len(result) == 1
    assert result[0].role == "diagnostic_agent"
```

### Integration Tests

```python
# ✅ Mock the model factory for graph tests
from unittest.mock import MagicMock

def test_graph_topology():
    graph = build_graph()
    # Verify edges exist without invoking LLMs
```

---

## Documentation Conventions

### Code Comments

```python
# ✅ Section divider with descriptive label
# ── ReAct loop ───────────────────────────────────────────────────────────────

# ✅ Inline comment explaining *why*, not *what*
# The recovery message goes OUTSIDE the for loop so that every tool_call_id
# in the batch already has its ToolMessage.

# ❌ Comments that restate the code
# Initialize count to zero  # NO — obvious from the code
```

### Architecture Decision Records

Significant design decisions are recorded in `ARCHITECTURE.md`. If a change reverses a previous decision, update the document and add a **Decision Log** entry at the end.
