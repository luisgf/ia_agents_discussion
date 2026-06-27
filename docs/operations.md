# Operations Guide

This document covers deployment, configuration, monitoring, security, and day-to-day operation of Agents Discussion.

---

## Table of Contents

- [Deployment](#deployment)
  - [Local Installation](#local-installation)
  - [Docker Deployment](#docker-deployment)
  - [Reverse Proxy Setup](#reverse-proxy-setup)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Model Selection](#model-selection)
  - [Prompt Templates](#prompt-templates)
- [Authentication](#authentication)
  - [GitHub Models](#github-models)
  - [GitHub Copilot](#github-copilot)
- [Runtime Management](#runtime-management)
  - [Starting the Web Server](#starting-the-web-server)
  - [CLI Execution](#cli-execution)
  - [Run Lifecycle](#run-lifecycle)
- [Tool Operations](#tool-operations)
  - [Approval Gating](#approval-gating)
  - [Tool Configuration](#tool-configuration)
  - [Adding Custom Tools](#adding-custom-tools)
- [Monitoring](#monitoring)
  - [Audit Logs](#audit-logs)
  - [Health Checks](#health-checks)
- [Troubleshooting](#troubleshooting)
- [Backup & Recovery](#backup--recovery)
- [Security Hardening](#security-hardening)
- [Maintenance](#maintenance)

---

## Deployment

### Local Installation

```bash
# 1. Clone and enter directory
cd /path/to/agents-discussion

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install in editable mode
pip install -e .

# 4. Copy and edit configuration
cp .env.example .env
# Edit .env with your tokens and preferences

# 5. Verify installation
agents-discuss --help
agents-discuss-web --help
```

### Docker Deployment

```bash
# 1. Build and start
cp .env.example .env  # edit as needed
docker compose up --build

# 2. Access the web UI
# http://localhost:8000 (or WEB_HOST:WEB_PORT from .env)
```

**Docker Compose Services:**

| Service | Description | Volume |
|---|---|---|
| `web` | FastAPI server | `/data` for run persistence |

**Persistent Data:**

The volume `/data` inside the container stores:
- Completed run JSON files
- `audit.jsonl` tool invocation log
- Custom prompt templates (if mounted)

### Reverse Proxy Setup

For production deployment behind a reverse proxy:

**Nginx Example:**

```nginx
server {
    listen 443 ssl http2;
    server_name diagnosis.example.com;

    ssl_certificate /etc/ssl/certs/example.com.crt;
    ssl_certificate_key /etc/ssl/private/example.com.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;  # SSE connections are long-lived
    }
}
```

**Important:** SSE connections require `proxy_read_timeout` to be high (default in Nginx is 60s, which kills streams).

---

## Configuration

### Environment Variables

All configuration is via environment variables or `.env` file. See `.env.example` for the full list.

#### Required

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | PAT for GitHub Models (if not using Copilot) |
| `DIAGNOSTIC_MODEL` | `copilot/gpt-4o` | Model for diagnosis agent |
| `SKEPTIC_MODEL` | `copilot/claude-sonnet-4.6` | Model for skeptic agent |
| `MODERATOR_MODEL` | `copilot/claude-sonnet-4.6` | Model for moderator |

#### Optional — Architecture

| Variable | Default | Description |
|---|---|---|
| `MAX_ROUNDS` | 4 | Maximum debate rounds (1–10) |
| `CONFIDENCE_THRESHOLD` | 0.8 | Threshold for moderator to close |
| `COMPRESS_HISTORY` | `true` | Enable history compression after round 2 |
| `SUMMARY_MODEL` | `""` | Model for history summaries (empty = moderator_model) |
| `EARLY_OUT_ENABLED` | `true` | Allow diagnostic agent to signal early closure |
| `EARLY_OUT_THRESHOLD` | 0.9 | Confidence threshold for early-out |

#### Optional — Tools

| Variable | Default | Description |
|---|---|---|
| `TOOLS_ENABLED` | `true` | Enable ReAct tools |
| `MAX_TOOL_CALLS_PER_AGENT` | 8 | Calls per agent per round |
| `ENABLED_TOOLS` | `""` | Comma-separated limit (empty = all) |
| `TOOL_APPROVAL_REQUIRED` | `true` | Require approval for sensitive tools |
| `APPROVAL_REQUIRED_TOOLS` | `run_ssh_command,run_local_command,run_kubectl,run_db_explain` | Which tools need approval |

#### Optional — Observability Endpoints

| Variable | Default | Description |
|---|---|---|
| `PROMETHEUS_URL` | — | Prometheus server for `query_prometheus` |
| `LOKI_URL` | — | Loki server for `query_loki` |
| `ELASTICSEARCH_URL` | — | Elasticsearch for `query_elasticsearch` |
| `DATABASE_URL` | — | Database for `run_db_explain` |

#### Optional — SSH

| Variable | Default | Description |
|---|---|---|
| `SSH_DEFAULT_USER` | `""` | Default SSH username |
| `SSH_KEY_PATH` | `""` | Default SSH private key |
| `SSH_CONNECT_TIMEOUT` | 15 | Connection timeout in seconds |

### Model Selection

Models are specified with optional provider prefix:

```bash
# GitHub Models (uses GITHUB_TOKEN)
DIAGNOSTIC_MODEL=openai/gpt-4o
SKEPTIC_MODEL=openai/gpt-4.1
MODERATOR_MODEL=openai/gpt-4o-mini

# GitHub Copilot (uses COPILOT_TOKEN, supports Claude/Gemini)
DIAGNOSTIC_MODEL=copilot/gpt-4o
SKEPTIC_MODEL=copilot/claude-sonnet-4.6
MODERATOR_MODEL=copilot/claude-sonnet-4.6
```

### Prompt Templates

Built-in templates:
- `default` — General diagnosis
- `performance` — Degradation and latency
- `errors` — 5xx errors and exceptions
- `data` — Data inconsistencies and corruption
- `security` — Security incidents

Override or add templates:

```bash
mkdir -p ~/.local/share/agents-discussion/prompts
cat > ~/.local/share/agents-discussion/prompts/custom.en.yaml << 'EOF'
name: custom
language: en
version: 1
description: Custom incident template
diagnostic_system: |
  You are a senior SRE diagnosing incidents...
skeptic_system: |
  You are a skeptical peer reviewer...
moderator_system: |
  You are a tech lead reviewing the debate...
EOF
```

---

## Authentication

### GitHub Models

1. Generate a PAT at https://github.com/settings/tokens
2. Ensure it has access to GitHub Models
3. Set `GITHUB_TOKEN=ghp_...` in `.env`

### GitHub Copilot

1. Run the auth helper once:

```bash
agents-discuss-copilot-auth
```

This initiates the OAuth device flow and stores the token in `~/.config/agents-discussion/copilot_token`.

2. Alternatively, copy the token directly to `.env`:

```bash
COPILOT_TOKEN=ghu_xxxxxxxxxxxxxxxxxxxx
```

Session tokens (TTL ~25 minutes) are managed automatically by `auth_copilot.py`.

---

## Runtime Management

### Starting the Web Server

```bash
# Default: 127.0.0.1:8000
agents-discuss-web

# Custom host/port
WEB_HOST=0.0.0.0 WEB_PORT=8080 agents-discuss-web
```

### CLI Execution

```bash
# Basic usage
agents-discuss "The /orders endpoint is slow"

# With context files
agents-discuss "Diagnose the issue" \
  --base-context architecture.md \
  --file incident.log

# With project context
agents-discuss "Debug slowness" \
  --project ./backend \
  --include "src/**/*.py" \
  --include "config/**/*.yaml"

# With custom thresholds
agents-discuss "Quick diagnosis" \
  --early-out-threshold 0.85 \
  --no-compress-history
```

### Run Lifecycle

| State | Description | Transitions |
|---|---|---|
| `running` | Debate in progress | → `completed`, `cancelled`, `error`, `interrupted` |
| `completed` | Moderator closed successfully | Final report available |
| `cancelled` | Operator stopped manually | Partial results preserved |
| `error` | Exception during execution | Error message in final event |
| `interrupted` | Server crashed during run | Marked on next startup |

### Resuming a Run

From the web UI or API:

```bash
POST /api/runs/{run_id}/resume
Content-Type: multipart/form-data

new_evidence=Additional logs from 2024-01-15
```

---

## Tool Operations

### Approval Gating

Sensitive tools block until an operator approves. The approval UI shows:

- Tool name and arguments
- Agent that requested it
- Approve / Reject buttons

**Timeout:** `APPROVAL_TIMEOUT_SECONDS` (default 300s). If no response, the tool is rejected and the agent continues without it.

### Tool Configuration

To disable all tools:

```bash
TOOLS_ENABLED=false
```

To allow only specific tools:

```bash
ENABLED_TOOLS=http_get,query_prometheus,git_recent_changes
```

### Adding Custom Tools

1. Create the tool function in `tools.py`:

```python
from langchain_core.tools import tool

@tool
def my_custom_check(
    endpoint: str,
    timeout: int = 10,
) -> str:
    """Run a custom health check on an internal service.

    Args:
        endpoint: Service URL to check.
        timeout: Request timeout in seconds.
    """
    import httpx
    r = httpx.get(endpoint, timeout=timeout)
    return f"Status: {r.status_code}\n{r.text[:2000]}"
```

2. Register it in `get_tools()`:

```python
def get_tools():
    return [
        run_ssh_command,
        http_get,
        # ... existing tools
        my_custom_check,
    ]
```

3. Optionally add to `APPROVAL_REQUIRED_TOOLS` if it has side effects.

---

## Monitoring

### Audit Logs

Every tool invocation is logged to `DATA_DIR/audit.jsonl`:

```json
{
  "timestamp": "2024-06-11T14:32:01Z",
  "run_id": "abc123",
  "agent": "diagnostic_agent",
  "tool": "run_ssh_command",
  "args": {"host": "prod-01", "command": "df -h"},
  "result": "Filesystem      Size  Used Avail Use%...",
  "error": false,
  "approval": "approved"
}
```

**Retention:** Rotate `audit.jsonl` with standard logrotate:

```bash
# /etc/logrotate.d/agents-discussion
/home/user/.local/share/agents-discussion/runs/audit.jsonl {
    daily
    rotate 30
    compress
    delaycompress
    missingok
}
```

### Health Checks

The web server exposes a basic health endpoint implicitly via FastAPI. For monitoring, check:

- `GET /api/runs` — Returns `200` if server is responsive
- SSE endpoint `/api/runs/{id}/events` — Stream connectivity

### Log Levels

Set `LOG_LEVEL` environment variable:

```bash
LOG_LEVEL=DEBUG agents-discuss-web  # Verbose model and tool logging
LOG_LEVEL=WARNING agents-discuss-web  # Only warnings and errors
```

---

## Troubleshooting

### Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: langchain_core` | Virtualenv not activated | `source .venv/bin/activate` |
| `No JSON object found in moderator response` | Moderator model returned malformed JSON | Check model compatibility; fallback path handles this |
| Tool approval timeout | Operator didn't respond | Reduce `APPROVAL_TIMEOUT_SECONDS` or disable approval |
| SSE stream disconnects | Nginx proxy_read_timeout too low | Set `proxy_read_timeout 86400;` |
| History compression fails | Summary model unavailable | Set `SUMMARY_MODEL` to a known working model |
| "name 'history' is not defined" | `_last_round_messages` bug after graph changes | Update to latest version where _last_round_messages counts forward |

### Diagnostic Steps

1. Check `.env` configuration:

```bash
cat .env | grep -v "^#" | grep -v "^$"
```

2. Verify token:

```bash
# GitHub Models
curl -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://models.github.ai/v1/models

# GitHub Copilot
agents-discuss-copilot-auth --verify
```

3. Test with minimal CLI run:

```bash
agents-discuss "Test topic" --no-redact-context
```

4. Check audit log for tool failures:

```bash
tail -20 ~/.local/share/agents-discussion/runs/audit.jsonl | jq .
```

---

## Backup & Recovery

### What to Back Up

| Path | Contents | Backup Frequency |
|---|---|---|
| `DATA_DIR/*.json` | Completed run records | Daily |
| `DATA_DIR/audit.jsonl` | Tool audit trail | Daily |
| `~/.local/share/agents-discussion/prompts/` | Custom templates | After changes |
| `~/.config/agents-discussion/copilot_token` | Copilot OAuth token | After generation |

### Restoration

```bash
# Restore runs
tar -xzf agents-discussion-backup-20240611.tar.gz -C ~/.local/share/

# Restart web server
agents-discuss-web
```

---

## Security Hardening

### Network

- Bind web server to `127.0.0.1` unless behind a reverse proxy.
- Use TLS 1.3 termination at the reverse proxy.
- Restrict SSH keys used by `run_ssh_command` to read-only operations.

### Secrets

- Never commit `.env` files to version control (`.gitignore` enforced).
- Redact secrets from context before sending to models (`--no-redact-context` override available).
- Rotate `GITHUB_TOKEN` and `COPILOT_TOKEN` quarterly.

### Tool Safety

- Always require approval for destructive tools (`run_local_command`, `run_ssh_command`).
- Use read-only database URLs for `run_db_explain`.
- Run with `ENABLED_TOOLS` limited to the minimum necessary set.

### Container Security

```dockerfile
# Dockerfile best practices
USER appuser  # Don't run as root
EXPOSE 8000
# No secrets in image layers
```

---

## Maintenance

### Regular Tasks

| Frequency | Task | Command |
|---|---|---|
| Weekly | Rotate audit log | `logrotate` |
| Monthly | Clean old interrupted runs | `find DATA_DIR -name "*.json" -mtime +30 -delete` |
| Quarterly | Update model versions | Edit `.env`, test, restart |
| Quarterly | Review custom templates | Check `PROMPTS_DIR` for drift |
| On demand | Re-index orphaned runs | Restart web server (triggers `_mark_orphans()`) |

### Upgrading

```bash
# Pull latest code
git pull origin main

# Reinstall dependencies
pip install -e .

# Restart services
pkill -f "agents-discuss-web"
agents-discuss-web
```

### Capacity Planning

| Resource | Baseline | Scaling Trigger |
|---|---|---|
| Disk | 1 GB / 1000 runs | Clean old runs |
| Memory | 150 MB per concurrent run | Increase `comment_timeout` to reduce concurrent |
| LLM tokens | ~50K tokens / round | Enable compression, lower `MAX_ROUNDS` |
| Network | Minimal (SSE streams) | Nginx worker processes |
