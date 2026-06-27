# Agents Discussion

Multi-agent automated root cause analysis for production incidents.

Agents Discussion simulates a structured SRE debate between specialized AI agents—each with distinct system prompts, model assignments, and tool access—to diagnose technical issues through iterative hypothesis generation, critique, rebuttal, and moderation. It combines the deep reasoning of multiple frontier language models with real-time tool invocation and human-in-the-loop oversight.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Orchestration](https://img.shields.io/badge/LangGraph-StateGraph-9cf.svg)

---

## Quick Links

| Document | Purpose |
|---|---|
| [ARCHITECTURE.md](docs/architecture.md) | System design, data flow, state schema, agent topology, decision rationale |
| [CODING_STYLE.md](docs/coding-style.md) | Python and JavaScript conventions, LangGraph patterns, project structure |
| [OPERATIONS.md](docs/operations.md) | Deployment, configuration, monitoring, security, troubleshooting |
| [USAGE.md](docs/usage.md) | CLI commands, web interface, API reference, templates, tool usage |

---

## What It Does

When a production incident occurs—a latency spike, 5xx errors, data inconsistency, or memory leak—Agents Discussion:

1. **Assembles a panel** of three specialized agents (diagnostician, skeptic, moderator), each running a potentially different frontier model.
2. **Probes infrastructure** via a ReAct tool loop: SSH, database EXPLAIN plans, Prometheus/Loki queries, Git history, HTTP health checks.
3. **Debates hypotheses** through multiple rounds, with each agent critiquing and refining proposed causes.
4. **Compresses history** after early rounds to keep token usage manageable while preserving relevant context.
5. **Routes adaptively** based on moderator flow directives—skipping phases when evidence is already conclusive.
6. **Terminates early** if a diagnostic agent reports overwhelming confidence in a specific fix.
7. **Involves operators** via tool approval gating and optional human-in-the-loop pause between rounds.
8. **Produces a report**: structured Markdown with full transcript, tool audit trail, hypothesis timeline, and executive summary.

---

## Installation

```bash
# Clone
git clone <repo-url>
cd agents-discussion

# Virtual environment
python -m venv .venv
source .venv/bin/activate

# Install
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your GITHUB_TOKEN or COPILOT_TOKEN
```

**Requirements:** Python 3.11+, Linux/macOS/WSL.

---

## Quick Start

### CLI

```bash
# Diagnose with minimal setup
agents-discuss "The /orders endpoint p95 latency is 2s since the 14:00 deploy"

# With context files
agents-discuss "Memory leak in worker pods" \
  --file logs/oom-kills.txt \
  --base-context docs/architecture.md
```

### Web Server

```bash
agents-discuss-web
# Open http://127.0.0.1:8000
```

The web interface provides:
- Real-time SSE stream of the debate
- Collapsible tool execution cards
- Tool approval controls
- Human-in-the-loop pause between rounds
- Export to Markdown report

---

## Architecture Overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌─────────────┐
│ Diagnostic  │────▶│   Skeptic    │────▶│  Rebuttal   │────▶│  Moderator  │
│   Agent     │     │    Agent     │     │    Agent    │     │    Agent    │
└──────┬──────┘     └──────────────┘     └─────────────┘     └──────┬──────┘
       │  ReAct tools                        ReAct tools              │
       │  (SSH, DB, Prometheus,…)           (SSH, DB, Prom,…)        │
       └──────────────────────────────────────────────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │ Summarize History  │ (round > 2)
                          └────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │  User Input Gate   │ (optional HITL)
                          └────────────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │  Route: repeat or  │
                          │  finalize report   │
                          └────────────────────┘
```

See [ARCHITECTURE.md](docs/architecture.md) for full data flow, state schema, and decision details.

---

## Key Features

- **Multi-model panel** — Assign different models (GPT-4o, Claude, Gemini) to each agent role.
- **ReAct tool loop** — Agents autonomously invoke SSH, SQL EXPLAIN, Prometheus, Loki, Git, HTTP, kubectl.
- **Tool approval gating** — Destructive commands block until an operator approves.
- **Structured hypotheses** — Agents track hypotheses through confirmed / active / rejected lifecycles.
- **Adaptive flow control** — Moderator can skip skeptic or rebuttal phases when evidence is conclusive.
- **History compression** — Rounds 3+ receive summarized prior history instead of full transcripts.
- **Early-out termination** — Diagnostic agent can propose early closure when confidence exceeds threshold.
- **Human-in-the-loop** — Optional pause between rounds for operator comments and new evidence.
- **Audit trail** — Every tool invocation logged in `audit.jsonl` with arguments, results, and approval status.

---

## Configuration

Configuration is entirely via environment variables or `.env` file:

| Variable | Purpose | Example |
|---|---|---|
| `GITHUB_TOKEN` | Authentication for GitHub Models | `ghp_xxxxxxxx` |
| `DIAGNOSTIC_MODEL` | Model for diagnosis agent | `copilot/gpt-4o` |
| `SKEPTIC_MODEL` | Model for skeptic agent | `copilot/claude-sonnet-4.6` |
| `MODERATOR_MODEL` | Model for moderator agent | `copilot/claude-sonnet-4.6` |
| `COMPRESS_HISTORY` | Enable history summarization | `true` |
| `EARLY_OUT_ENABLED` | Enable early-out termination | `true` |
| `TOOL_APPROVAL_REQUIRED` | Require approval for sensitive tools | `true` |

See [OPERATIONS.md](docs/operations.md) for complete configuration reference.

---

## API

The web server exposes a full REST API consumed by the web UI:

```http
POST /api/runs               # Start a debate
GET  /api/runs               # List runs
GET  /api/runs/{id}          # Get run details
GET  /api/runs/{id}/events   # SSE stream
POST /api/runs/{id}/resume   # Resume with new evidence
POST /api/runs/{id}/approval # Resolve tool approval
POST /api/runs/{id}/comment  # Human-in-the-loop comment
GET  /api/models             # Available models
GET  /api/prompts            # Available templates
GET  /api/settings           # Current settings
```

See [USAGE.md](docs/usage.md) for full API reference and examples.

---

## Operation

```bash
# Start web server
agents-discuss-web

# CLI with full context
agents-discuss "Latency spike" \
  --file logs/trace.json \
  --base-context docs/architecture.md \
  --project ./backend \
  --include "src/**/*.py"
```

See [OPERATIONS.md](docs/operations.md) for deployment, monitoring, and troubleshooting.

---

## Documentation Structure

| File | Audience | Content |
|---|---|---|
| `README.md` | Everyone | Overview, quickstart, links |
| `docs/architecture.md` | Developers, architects | Design, state flow, schema, decisions |
| `docs/coding-style.md` | Contributors | Conventions, patterns, project structure |
| `docs/operations.md` | DevOps, SREs | Deploy, config, monitor, troubleshoot |
| `docs/usage.md` | End users | CLI, web UI, API, templates, tools |
| `docs/reference/` | Everyone | Generated from code: API, CLI, config, tools, events |

The full documentation is published at **<https://luisgf.github.io/ia_agents_discussion/>**.

---

## Contributing

1. Read [CODING_STYLE.md](docs/coding-style.md) for conventions.
2. Review [ARCHITECTURE.md](docs/architecture.md) for design rationale.
3. Run `ruff check .` and `ruff format --check .` before committing.

---

## License

This project is licensed under the **GNU General Public License v3.0 or later
(GPL-3.0-or-later)**. See the [LICENSE](LICENSE) file for the full text.

Copyright (C) 2025 Luis González Fernández

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE.
