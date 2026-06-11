# Usage Guide

Complete guide for using Agents Discussion via CLI, Web UI, and API.

---

## Table of Contents

- [Quick Start](#quick-start)
- [CLI Usage](#cli-usage)
  - [Basic Commands](#basic-commands)
  - [Context Options](#context-options)
  - [Project Context](#project-context)
  - [History Display](#history-display)
- [Web Interface](#web-interface)
  - [Starting a Debate](#starting-a-debate)
  - [Live View](#live-view)
  - [Tool Approval](#tool-approval)
  - [Human-in-the-Loop](#human-in-the-loop)
  - [Exporting Reports](#exporting-reports)
  - [Resuming a Debate](#resuming-a-debate)
- [API Reference](#api-reference)
  - [Runs](#runs)
  - [Events (SSE)](#events-sse)
  - [Prompts](#prompts)
  - [Settings](#settings)
  - [Models](#models)
- [Prompt Templates](#prompt-templates)
- [Working with Tools](#working-with-tools)
- [Interpreting Results](#interpreting-results)
  - [Moderator Decision States](#moderator-decision-states)
  - [Confidence Scores](#confidence-scores)
  - [Hypothesis Timeline](#hypothesis-timeline)

---

## Quick Start

### Installation

```bash
# Clone repository
git clone <repo-url>
cd agents-discussion

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your GITHUB_TOKEN or COPILOT_TOKEN
```

### First CLI Run

```bash
agents-discuss "The /api/users endpoint returns 500ms latency since yesterday"
```

### First Web Run

```bash
agents-discuss-web
# Open http://127.0.0.1:8000 in your browser
```

---

## CLI Usage

### Basic Commands

```bash
# Minimal — topic only
agents-discuss "Slow database queries on production"

# With incident file
agents-discuss "Investigate memory leak" --file gc-logs.txt

# With base context (architecture docs)
agents-discuss "Service degradation" \
  --base-context docs/architecture.md \
  --base-context docs/slos.md

# Combine topic + file + base context
agents-discuss "Diagnose API slowness" \
  --file logs/api-error.log \
  --base-context docs/services.md
```

### Context Options

| Flag | Description | Repeatable |
|---|---|---|
| `--file`, `-f` | Incident file (logs, traces, metrics) | No |
| `--base-context` | Architecture/constraint files | Yes |
| `--project` | Source directory for code context | No |
| `--include` | Glob pattern for project files | Yes |
| `--no-redact-context` | Disable secret redaction | No |

### Project Context

```bash
# Include common project files automatically
agents-discuss "Fix race condition" --project ./my-app

# Custom patterns (override defaults)
agents-discuss "Memory issue" \
  --project ./backend \
  --include "src/**/*.py" \
  --include "config/**/*.yaml" \
  --max-files 30 \
  --max-chars-per-file 15000

# Defaults included:
# README*, pyproject.toml, requirements*.txt, package.json,
# tsconfig.json, go.mod, Cargo.toml, Dockerfile,
# src/**/*, tests/**
```

### History Display

```bash
# Show full debate history after result
agents-discuss "Debug timeout" --show-history

# Show everything including hidden tool calls
agents-discuss "Full investigation" \
  --project ./app \
  --show-history
```

### CLI Flags Summary

```
usage: agents-discuss [-h] [--file FILE] [--base-context BASE_CONTEXT]
                      [--no-redact-context] [--project PROJECT]
                      [--include INCLUDE] [--max-files MAX_FILES]
                      [--max-chars-per-file MAX_CHARS_PER_FILE]
                      [--show-history] [--no-compress-history]
                      [--early-out-threshold EARLY_OUT_THRESHOLD]
                      [topic]

positional arguments:
  topic                 Technical issue to diagnose

optional arguments:
  -h, --help            show this help message and exit
  --file FILE, -f FILE  File with logs/traces/metrics
  --base-context BASE_CONTEXT
                        Architecture/constraint file (repeatable)
  --no-redact-context   Don't redact secrets from context
  --project PROJECT     Directory with source files
  --include INCLUDE     Glob pattern for project (repeatable)
  --max-files MAX_FILES
                        Max project files (default: 20)
  --max-chars-per-file MAX_CHARS_PER_FILE
                        Max chars per file (default: 12000)
  --show-history        Print all agent turns after result
  --no-compress-history Disable history compression between rounds
  --early-out-threshold EARLY_OUT_THRESHOLD
                        Confidence for early-out
```

---

## Web Interface

### Starting a Debate

1. Open the web UI at `http://127.0.0.1:8000`
2. Fill in the **Topic** field (e.g., "Slow /orders endpoint")
3. (Optional) Upload an **Incident File** and/or **Base Context Files**
4. (Optional) Set **Project Path** for code context
5. Select **Template** and **Language**
6. Choose **Models per agent** (or leave defaults)
7. Toggle **Tool Approval** and **Pause Between Rounds**
8. Click **Start Diagnosis**

### Live View

The live view shows:
- **Streaming text** from each agent token-by-token
- **Tool calls** grouped in collapsible cards with execution status
- **Approval requests** blocking execution until resolved
- **Moderator decisions** with confidence, risk, and flow directives
- **Round separators** marking debate progression

**Event colors:**
- 🔵 Diagnostic agent — proposals and experiments
- ⚡ Skeptic agent — critiques and alternatives
- 🛡️ Rebuttal agent — responses and refinements
- ⚖️ Moderator — decisions and routing
- 🛠️ Tools — grouped execution cards
- 👤 User — operator comments (HITL)

### Tool Approval

When a sensitive tool is invoked, the UI shows:

```
⚠️ Aprobación requerida
run_ssh_command on prod-01
Command: ps aux | grep python
Agent: Diagnóstico Principal
[Aprobar] [Rechazar]
```

**Auto timeout:** If no response within `APPROVAL_TIMEOUT_SECONDS`, the tool is rejected.

### Human-in-the-Loop

With **Pause Between Rounds** enabled:

1. After each moderator decision with `status: continue`, the debate pauses.
2. A comment box appears in the UI.
3. Enter additional evidence or questions.
4. Click **Continue** to inject the comment and proceed.

**Use cases:**
- Provide new logs mid-debate
- Correct a false assumption
- Request a specific diagnostic angle

### Exporting Reports

After a debate completes:

1. Click **Export Report** in the post-run toolbar.
2. A Markdown file downloads containing:
   - Complete debate transcript
   - All tool invocations with results
   - Hypothesis evolution timeline
   - Final moderator decision
   - Structured executive summary

### Resuming a Debate

When a moderator closes with `status: needs_more_data`:

1. Go to the **History** panel.
2. Find the run and click **Resume**.
3. Provide new evidence (text or files).
4. The new debate starts from the full prior history plus new evidence.

---

## API Reference

All API endpoints return JSON. The web UI is a client of this API.

### Runs

#### Create Run

```http
POST /api/runs
Content-Type: multipart/form-data

topic=Slow%20endpoint&
diagnostic_model=copilot%2Fgpt-4o&
skeptic_model=copilot%2Fclaude-sonnet-4.6&
moderator_model=copilot%2Fclaude-sonnet-4.6&
template=performance&
language=en&
pause_between_rounds=false&
require_approval=true&
project_path=%2Fpath%2Fto%2Fproject&
max_files=20
```

**Response:**
```json
{
  "run_id": "a1b2c3d4e5f6"
}
```

#### List Runs

```http
GET /api/runs
```

**Response:**
```json
{
  "runs": [
    {
      "run_id": "a1b2c3d4e5f6",
      "topic": "Slow endpoint",
      "timestamp": "2024-06-11T14:32:01Z",
      "status": "completed",
      "models": {
        "diagnostic": "copilot/gpt-4o",
        "skeptic": "copilot/claude-sonnet-4.6",
        "moderator": "copilot/claude-sonnet-4.6"
      },
      "template": "performance",
      "language": "en",
      "parent_run_id": null
    }
  ]
}
```

#### Get Run

```http
GET /api/runs/{run_id}
```

**Response:** Full run record with events (no SSE streaming).

#### Delete Run

```http
DELETE /api/runs/{run_id}
```

#### Resume Run

```http
POST /api/runs/{run_id}/resume
Content-Type: multipart/form-data

new_evidence=Additional%20logs%20here
```

#### Resolve Tool Approval

```http
POST /api/runs/{run_id}/approval
Content-Type: application/json

{
  "call_id": "abc123def456",
  "approved": true
}
```

#### Submit HITL Comment

```http
POST /api/runs/{run_id}/comment
Content-Type: application/json

{
  "comment": "The connection pool size was reduced in the last deploy."
}
```

### Events (SSE)

```http
GET /api/runs/{run_id}/events
Accept: text/event-stream
```

**Event stream format:**

```
event: message
data: {"type":"run_started","round":1,"topic":"Slow endpoint",...}

event: message
data: {"type":"agent_turn_started","agent_node":"diagnostic_agent",...}

event: message
data: {"type":"agent_delta","agent_node":"diagnostic_agent","delta":"Based on"}

event: message
data: {"type":"agent_completed","node":"diagnostic_agent","role":"Diagnóstico Principal","content":"..."}

event: message
data: {"type":"moderator_decision","node":"moderator_agent","decision":{"status":"continue",...},...}

event: message
data: {"type":"run_finished"}
```

**Client implementation (JavaScript):**

```javascript
const source = new EventSource(`/api/runs/${runId}/events`);
source.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(data.type, data);
};
source.onerror = () => {
  source.close();
};
```

### Prompts

```http
GET /api/prompts
```

**Response:**
```json
{
  "templates": [
    {
      "name": "default",
      "language": "es",
      "version": 1,
      "description": "Diagnóstico general",
      "source": "builtin"
    },
    {
      "name": "performance",
      "language": "en",
      "version": 1,
      "description": "Performance degradation and latency",
      "source": "builtin"
    }
  ]
}
```

### Settings

```http
GET /api/settings
```

**Response:**
```json
{
  "diagnostic_model": "copilot/gpt-4o",
  "skeptic_model": "copilot/claude-sonnet-4.6",
  "moderator_model": "copilot/claude-sonnet-4.6",
  "summary_model": "",
  "max_rounds": 4,
  "confidence_threshold": 0.8,
  "early_out_threshold": 0.9,
  "prompt_template": "default",
  "prompt_language": "es",
  "tool_approval_required": true,
  "compress_history": true
}
```

### Models

```http
GET /api/models
```

Returns available models from GitHub Models and Copilot endpoints.

---

## Prompt Templates

Templates define the system prompts for each agent role. They are versioned YAML files.

### Built-in Templates

| Template | Spanish | English | Focus |
|---|---|---|---|
| `default` | ✅ | ✅ | General diagnosis |
| `performance` | ✅ | ✅ | Latency, throughput, resource usage |
| `errors` | ✅ | ✅ | 5xx errors, exceptions, crashes |
| `data` | ✅ | ✅ | Data inconsistency, corruption, duplicates |
| `security` | ✅ | ✅ | Security incidents, anomalous access |

### Template Format

```yaml
name: custom
language: en
version: 1
description: Custom template for my org
diagnostic_system: |
  You are an expert SRE at ACME Corp. You specialize in diagnosing
  issues with our microservices running on Kubernetes.

  Your infrastructure uses:
  - PostgreSQL for primary data
  - Redis for caching
  - Kafka for event streaming

  When proposing hypotheses, consider these architectural constraints.

skeptic_system: |
  You are a senior engineer reviewing the diagnosis.
  Challenge every claim that lacks instrumental evidence.
  Always check PostgreSQL query plans and Redis memory usage.

moderator_system: |
  You are the platform lead. Close the debate when:
  - A fix is clear and reversible
  - Or we have identified the exact remediation step
  - Or we are blocked by missing evidence
```

### Custom Template Directory

```bash
# Create custom template directory
mkdir -p ~/.local/share/agents-discussion/prompts

# Add custom template
cat > ~/.local/share/agents-discussion/prompts/my-org.en.yaml

# Set in .env or use in CLI
PROMPTS_DIR=/home/user/.local/share/agents-discussion/prompts
agents-discuss "Issue" --template my-org --language en
```

---

## Working with Tools

### Available Tools

| Tool | Description | Auto-approval |
|---|---|---|
| `http_get` | GET health endpoints / APIs | ✅ Yes |
| `query_prometheus` | PromQL instant queries | ✅ Yes |
| `query_loki` | LogQL range queries | ✅ Yes |
| `query_elasticsearch` | Read-only `_search` | ✅ Yes |
| `git_recent_changes` | Recent commits + diffstat | ✅ Yes |
| `run_db_explain` | PostgreSQL EXPLAIN plans | ❌ Requires approval |
| `run_local_command` | Shell commands on host | ❌ Requires approval |
| `run_ssh_command` | SSH commands on remote | ❌ Requires approval |
| `run_kubectl` | Read-only kubectl | ❌ Requires approval |

### Tool Usage Patterns

**Database diagnosis:**

```
diagnostic_agent → query_prometheus("avg(pg_stat_activity_count)")
                → run_db_explain("SELECT * FROM orders WHERE created_at > '2024-01-01'")
                → http_get("http://db-metrics:8080/metrics")
```

**Deployment correlation:**

```
diagnostic_agent → git_recent_changes(" HEAD~5..HEAD")
                → http_get("http://api/health")
                → query_loki('{app="api"} |= "error"')
```

**Infrastructure check:**

```
diagnostic_agent → run_ssh_command("prod-01", "df -h /var/log")
                → run_kubectl("kubectl top pods -n production")
                → query_prometheus("node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes")
```

### Tool Approval Decisions

**When to Approve:**
- Read-only diagnostic commands (`ps`, `df`, `netstat`, `EXPLAIN`)
- Health endpoint checks
- Log/metric queries

**When to Reject:**
- Commands that modify state (`rm`, `kill`, `UPDATE`, `DELETE`)
- Commands accessing sensitive data unnecessarily
- Commands on production without change control approval

---

## Interpreting Results

### Moderator Decision States

| Status | Meaning | Next Action |
|---|---|---|
| `continue` | More investigation needed | Next round starts |
| `final_diagnosis` | Root cause identified | Fix recommended |
| `needs_more_data` | Ambiguous, requires evidence | Resume with new data |
| `propose_fix` | Clear minimal fix available | Apply fix and validate |
| `structured_uncertainty` | Cannot determine with available info | Document uncertainty |

### Confidence Scores

| Range | Interpretation |
|---|---|
| 0.0 – 0.4 | Low confidence; multiple hypotheses plausible |
| 0.4 – 0.7 | Moderate confidence; leading hypothesis emerging |
| 0.7 – 0.85 | High confidence; strong evidence for leading cause |
| 0.85 – 1.0 | Very high confidence; conclusive evidence |

**Important:** The moderator's `status` overrides raw confidence. A `continue` with 0.9 confidence means the moderator believes more evidence would strengthen the fix proposal, not that the diagnosis is wrong.

### Hypothesis Timeline

The report shows hypothesis evolution:

```markdown
## Hipótesis en debate
- H-1 [confirmed]: Missing index on orders.created_at
  · EXPLAIN shows sequential scan on 10M rows
- H-2 [rejected]: Network latency between services
  · ping shows <1ms; no evidence supported
- H-3 [active]: Connection pool exhaustion
  · Needs evidence: current pool size vs usage
```

### Early-Out Indicators

When a debate concludes early:

```
⚠️ Debate concluded early in round 1
Rationale: EXPLAIN output shows conclusive missing index
Confidence: 0.97
Action: Apply CREATE INDEX CONCURRENTLY
```

This is shown in the UI as an info card and in the report header.

### Flow Directives

When the moderator skips phases:

```
⚠️ Revisor Escéptico omitido
Razón: Evidence is conclusive; no ambiguity to falsify
```

This indicates the moderator short-circuited the standard flow, usually due to early-out or low-value critique.

---

## Examples

### Example 1: Production Latency Spike

```bash
agents-discuss "The /checkout endpoint p95 latency increased from 200ms to 2s after the 14:00 deploy" \
  --base-context docs/architecture.md \
  --project ./backend \
  --template performance
```

**Expected output:**
- Diagnostic queries database query plans
- Skeptic checks for caching layer issues
- Moderator decides between missing index (confirmed) or cache invalidation (rejected)

### Example 2: Intermittent 5xx Errors

```bash
agents-discuss "Users report random 502 Bad Gateway on /api/search" \
  --file logs/gateway-errors.log \
  --template errors
```

**Expected output:**
- Diagnostic checks upstream health
- Skeptic proposes load balancer misconfiguration
- Moderator identifies Nginx worker exhaustion as root cause

### Example 3: Data Inconsistency

```bash
agents-discuss "Duplicate orders appearing in the dashboard" \
  --project ./orders-service \
  --template data \
  --include "src/**/*.sql" \
  --include "migrations/**/*.sql"
```

**Expected output:**
- Diagnostic finds race condition in order creation
- Skeptic checks for idempotency key failure
- Moderator recommends adding unique constraint + retry logic

---

## Keyboard Shortcuts (Web)

| Key | Action |
|---|---|
| `Enter` | Submit HITL comment |
| `Esc` | Close approval dialog |
| `Ctrl + E` | Export current report |
| `Ctrl + N` | Start new debate |
