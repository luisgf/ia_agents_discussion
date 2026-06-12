"""
Diagnostic tools available to all agents during the debate.

Tools:
  - run_ssh_command     : execute a command on a remote host via SSH
  - http_get            : HTTP GET request (health-checks, internal APIs)
  - run_local_command   : execute a read-only shell command locally
  - query_prometheus    : instant PromQL query against a Prometheus server
  - query_loki          : LogQL range query against a Loki server
  - query_elasticsearch : read-only _search against an Elasticsearch index
  - run_kubectl         : read-only kubectl commands (get/describe/logs/...)
  - run_db_explain      : EXPLAIN of a SELECT via psql (plan only, no execution)
  - git_recent_changes  : recent commits + diffstat of a repo (deploy diff)

Endpoints default from environment variables: PROMETHEUS_URL, LOKI_URL,
ELASTICSEARCH_URL, DATABASE_URL.

Tools listed in APPROVAL_REQUIRED_TOOLS (settings) need operator approval
when the run was started from the web UI with approval gating enabled.
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import time

import httpx
import paramiko
from langchain_core.tools import tool

from agents_discussion.config import get_settings

# Maximum characters kept from any tool output (prevents prompt bloat)
_MAX_OUT = 4_000


def _truncate(text: str) -> str:
    """Keep head + tail within the budget: the end of logs/commands often carries the signal."""
    if len(text) <= _MAX_OUT:
        return text
    head = (_MAX_OUT * 2) // 3
    tail = _MAX_OUT - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n... [truncated — {omitted} of {len(text)} chars omitted] ...\n" + text[-tail:]


def _ca_bundle() -> str | bool:
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


def _run_argv(argv: list[str], timeout: int) -> str:
    """Run a command without a shell and return formatted stdout/stderr."""
    try:
        t0 = time.monotonic()
        result = subprocess.run(  # noqa: S603 — argv form, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        parts = [f"# $ {' '.join(argv)}  (exit {result.returncode}, {elapsed} ms)"]
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        return _truncate("\n".join(parts))
    except FileNotFoundError:
        return f"Error: '{argv[0]}' is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {' '.join(argv)}"
    except Exception as exc:  # noqa: BLE001
        return f"Error executing command: {exc}"


# ── SSH ─────────────────────────────────────────────────────────────────────


def _resolve_ssh_key(key_path: str) -> tuple[str | None, str]:
    """Resolve the SSH key file to use, guarding against nonexistent paths.

    The LLM sometimes passes a key_path copied (or mistyped) from the context.
    Returns (key_filename, note): the path to hand to paramiko (None → let it
    auto-discover ~/.ssh/id_* and the agent) and a note describing any
    substitution. A note starting with "SSH key file not found" means no usable
    key was found and the caller should fail fast with that message.
    """
    requested = os.path.expanduser(key_path) if key_path else ""
    if requested and os.path.exists(requested):
        return requested, ""

    default_raw = os.environ.get("SSH_KEY_PATH", "")
    default = os.path.expanduser(default_raw) if default_raw else ""
    if default and os.path.exists(default):
        if requested:
            return default, (
                f"[nota: key_path '{key_path}' no existe; usando la clave por defecto "
                f"'{default_raw}'. No vuelvas a pasar esa ruta.]\n"
            )
        return default, ""

    if requested:
        extra = f" (default SSH_KEY_PATH '{default_raw}' not found either)" if default_raw else ""
        return None, (
            f"SSH key file not found: '{key_path}'{extra}. "
            "Omite key_path para usar los defaults del sistema (~/.ssh/id_* o ssh-agent)."
        )
    # Nothing requested and no (valid) default: let paramiko auto-discover.
    return None, ""


@tool
def run_ssh_command(
    host: str,
    command: str,
    user: str = "",
    key_path: str = "",
    port: int = 22,
    timeout: int = 15,
) -> str:
    """Execute a shell command on a remote server via SSH and return stdout + stderr.

    Args:
        host:     IP or hostname of the remote server.
        command:  Shell command to execute (e.g. "ps aux | grep python").
        user:     SSH username. Falls back to SSH_DEFAULT_USER env var.
        key_path: Path to the private key file. Leave empty to use the configured
                  default (SSH_KEY_PATH) or the system keys; only set it to
                  override them with a path you know exists.
        port:     SSH port (default 22).
        timeout:  Connection + execution timeout in seconds (default 15).
    """
    user = user or os.environ.get("SSH_DEFAULT_USER") or getpass.getuser()

    if not host:
        return "Error: 'host' is required."

    key_filename, key_note = _resolve_ssh_key(key_path)
    if key_filename is None and key_note:
        return key_note

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        t0 = time.monotonic()
        client.connect(
            hostname=host,
            port=port,
            username=user,
            key_filename=key_filename,
            look_for_keys=True,
            allow_agent=True,
            timeout=timeout,
            auth_timeout=timeout,
        )
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        elapsed = round((time.monotonic() - t0) * 1000)
        client.close()

        parts = []
        if out:
            parts.append(out.rstrip())
        if err:
            parts.append(f"[stderr]\n{err.rstrip()}")
        result = "\n".join(parts) if parts else "(no output)"
        return _truncate(f"{key_note}# {user}@{host} $ {command}  ({elapsed} ms)\n{result}")

    except paramiko.AuthenticationException as exc:
        return f"SSH authentication failed for {user}@{host}: {exc}"
    except paramiko.SSHException as exc:
        return f"SSH error connecting to {host}: {exc}"
    except OSError as exc:
        return f"Network error reaching {host}:{port}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Unexpected SSH error: {exc}"
    finally:
        client.close()


# ── HTTP ────────────────────────────────────────────────────────────────────


@tool
def http_get(url: str, timeout: int = 10) -> str:
    """Make an HTTP GET request and return the status code and response body.

    Useful for checking health endpoints, internal APIs, or metrics pages.

    Args:
        url:     Full URL including scheme (e.g. "http://service:8080/health").
        timeout: Request timeout in seconds (default 10).
    """
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    try:
        t0 = time.monotonic()
        r = httpx.get(url, timeout=timeout, follow_redirects=True, verify=_ca_bundle())
        elapsed = round((time.monotonic() - t0) * 1000)
        body = r.text
        header = f"HTTP {r.status_code}  {url}  ({elapsed} ms)\n"
        return _truncate(header + body)
    except httpx.TimeoutException:
        return f"Timeout after {timeout}s reaching {url}"
    except httpx.RequestError as exc:
        return f"Request error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Unexpected HTTP error: {exc}"


# ── Local shell ─────────────────────────────────────────────────────────────


@tool
def run_local_command(command: str, timeout: int = 15) -> str:
    """Execute a diagnostic shell command on the local machine and return its output.

    Suitable for read-only commands such as: ps, netstat, ss, df, free, top,
    journalctl, tail, grep, cat, curl, ping, dig, etc.

    Args:
        command: Shell command string (runs via /bin/sh -c).
        timeout: Execution timeout in seconds (default 15).
    """
    if not command.strip():
        return "Error: empty command."

    try:
        t0 = time.monotonic()
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        out = result.stdout or ""
        err = result.stderr or ""
        parts = [f"# local $ {command}  (exit {result.returncode}, {elapsed} ms)"]
        if out:
            parts.append(out.rstrip())
        if err:
            parts.append(f"[stderr]\n{err.rstrip()}")
        return _truncate("\n".join(parts))
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"
    except Exception as exc:  # noqa: BLE001
        return f"Error executing command: {exc}"


# ── Prometheus ──────────────────────────────────────────────────────────────


@tool
def query_prometheus(query: str, base_url: str = "", timeout: int = 15) -> str:
    """Run an instant PromQL query against a Prometheus server.

    Examples: 'rate(http_requests_total[5m])', 'up', 'node_memory_MemAvailable_bytes'.

    Args:
        query:    PromQL expression.
        base_url: Prometheus base URL (e.g. "http://prometheus:9090").
                  Falls back to the PROMETHEUS_URL env var.
        timeout:  Request timeout in seconds (default 15).
    """
    base_url = (base_url or os.environ.get("PROMETHEUS_URL", "")).rstrip("/")
    if not base_url:
        return "Error: no Prometheus URL. Pass base_url or set PROMETHEUS_URL."
    try:
        r = httpx.get(
            f"{base_url}/api/v1/query",
            params={"query": query},
            timeout=timeout,
            verify=_ca_bundle(),
        )
        if r.status_code != 200:
            return _truncate(f"Prometheus HTTP {r.status_code}: {r.text}")
        data = r.json()
        if data.get("status") != "success":
            return _truncate(f"Prometheus error: {json.dumps(data, ensure_ascii=False)}")
        results = data.get("data", {}).get("result", [])
        if not results:
            return f"Prometheus query '{query}': no results."
        lines = [f"# promql: {query}  ({len(results)} series)"]
        for item in results[:25]:
            metric = item.get("metric", {})
            value = item.get("value") or item.get("values")
            lines.append(f"{json.dumps(metric, ensure_ascii=False)} => {value}")
        if len(results) > 25:
            lines.append(f"... ({len(results) - 25} more series omitted)")
        return _truncate("\n".join(lines))
    except httpx.TimeoutException:
        return f"Timeout after {timeout}s reaching Prometheus at {base_url}"
    except Exception as exc:  # noqa: BLE001
        return f"Prometheus query error: {exc}"


# ── Loki ────────────────────────────────────────────────────────────────────


@tool
def query_loki(
    query: str,
    base_url: str = "",
    minutes: int = 15,
    limit: int = 50,
    timeout: int = 15,
) -> str:
    """Run a LogQL range query against a Grafana Loki server over the last N minutes.

    Example queries: '{app="orders"} |= "error"', '{namespace="prod"} | json | status >= 500'.

    Args:
        query:    LogQL expression.
        base_url: Loki base URL (e.g. "http://loki:3100"). Falls back to LOKI_URL env var.
        minutes:  Look-back window in minutes (default 15).
        limit:    Maximum log lines to return (default 50).
        timeout:  Request timeout in seconds (default 15).
    """
    base_url = (base_url or os.environ.get("LOKI_URL", "")).rstrip("/")
    if not base_url:
        return "Error: no Loki URL. Pass base_url or set LOKI_URL."
    now_ns = int(time.time() * 1e9)
    start_ns = now_ns - int(minutes * 60 * 1e9)
    try:
        r = httpx.get(
            f"{base_url}/loki/api/v1/query_range",
            params={"query": query, "start": start_ns, "end": now_ns, "limit": limit},
            timeout=timeout,
            verify=_ca_bundle(),
        )
        if r.status_code != 200:
            return _truncate(f"Loki HTTP {r.status_code}: {r.text}")
        data = r.json()
        streams = data.get("data", {}).get("result", [])
        if not streams:
            return f"Loki query '{query}' (last {minutes}m): no log lines."
        lines = [f"# logql: {query}  (last {minutes}m)"]
        for stream in streams:
            labels = json.dumps(stream.get("stream", {}), ensure_ascii=False)
            lines.append(f"--- {labels}")
            for _ts, line in stream.get("values", [])[:limit]:
                lines.append(line)
        return _truncate("\n".join(lines))
    except httpx.TimeoutException:
        return f"Timeout after {timeout}s reaching Loki at {base_url}"
    except Exception as exc:  # noqa: BLE001
        return f"Loki query error: {exc}"


# ── Elasticsearch ───────────────────────────────────────────────────────────


@tool
def query_elasticsearch(
    index: str,
    query_json: str,
    base_url: str = "",
    size: int = 20,
    timeout: int = 15,
) -> str:
    """Run a read-only _search query against an Elasticsearch/OpenSearch index.

    Args:
        index:      Index name or pattern (e.g. "logs-app-*").
        query_json: JSON string with the ES query DSL body, e.g.
                    '{"query": {"match": {"level": "error"}}}'.
        base_url:   Cluster base URL (e.g. "http://elasticsearch:9200").
                    Falls back to ELASTICSEARCH_URL env var.
        size:       Maximum hits to return (default 20).
        timeout:    Request timeout in seconds (default 15).
    """
    base_url = (base_url or os.environ.get("ELASTICSEARCH_URL", "")).rstrip("/")
    if not base_url:
        return "Error: no Elasticsearch URL. Pass base_url or set ELASTICSEARCH_URL."
    try:
        body = json.loads(query_json) if query_json.strip() else {}
    except json.JSONDecodeError as exc:
        return f"Error: query_json is not valid JSON: {exc}"
    body.setdefault("size", min(size, 50))
    try:
        r = httpx.post(
            f"{base_url}/{index}/_search",
            json=body,
            timeout=timeout,
            verify=_ca_bundle(),
        )
        if r.status_code != 200:
            return _truncate(f"Elasticsearch HTTP {r.status_code}: {r.text}")
        data = r.json()
        hits = data.get("hits", {})
        total = hits.get("total", {})
        total_val = total.get("value", total) if isinstance(total, dict) else total
        lines = [f"# es _search {index}  (total: {total_val})"]
        for hit in hits.get("hits", []):
            lines.append(json.dumps(hit.get("_source", {}), ensure_ascii=False))
        if "aggregations" in data:
            lines.append("[aggregations]")
            lines.append(json.dumps(data["aggregations"], ensure_ascii=False))
        return _truncate("\n".join(lines))
    except httpx.TimeoutException:
        return f"Timeout after {timeout}s reaching Elasticsearch at {base_url}"
    except Exception as exc:  # noqa: BLE001
        return f"Elasticsearch query error: {exc}"


# ── kubectl (read-only) ─────────────────────────────────────────────────────

_KUBECTL_READONLY_VERBS = {
    "get", "describe", "logs", "top", "events",
    "explain", "version", "api-resources", "cluster-info", "config",
}


@tool
def run_kubectl(kubectl_args: str, timeout: int = 20) -> str:
    """Run a READ-ONLY kubectl command and return its output.

    Allowed verbs: get, describe, logs, top, events, explain, version,
    api-resources, cluster-info, config (view). Mutating verbs (apply,
    delete, edit, exec, scale, ...) are rejected.

    Args:
        kubectl_args: kubectl arguments WITHOUT the 'kubectl' prefix,
                      e.g. "get pods -n prod -o wide" or "logs deploy/orders -n prod --tail=100".
        timeout:      Execution timeout in seconds (default 20).
    """
    try:
        argv = shlex.split(kubectl_args)
    except ValueError as exc:
        return f"Error parsing arguments: {exc}"
    if not argv:
        return "Error: empty kubectl arguments."

    verb = argv[0]
    if verb not in _KUBECTL_READONLY_VERBS:
        return (
            f"Error: kubectl verb '{verb}' is not allowed. "
            f"Read-only verbs only: {', '.join(sorted(_KUBECTL_READONLY_VERBS))}."
        )
    if verb == "config" and (len(argv) < 2 or argv[1] != "view"):
        return "Error: only 'kubectl config view' is allowed."

    return _run_argv(["kubectl", *argv], timeout)


# ── Database EXPLAIN ────────────────────────────────────────────────────────


@tool
def run_db_explain(query: str, database_url: str = "", timeout: int = 20) -> str:
    """Show the PostgreSQL execution plan (EXPLAIN) for a SELECT query.

    Only SELECT/WITH queries are accepted and EXPLAIN does NOT execute the
    query (no ANALYZE), so this is safe and read-only. Requires the psql
    client to be installed.

    Args:
        query:        The SELECT query to explain (without the EXPLAIN keyword).
        database_url: Connection string (postgres://user:pass@host:port/db).
                      Falls back to the DATABASE_URL env var.
        timeout:      Execution timeout in seconds (default 20).
    """
    database_url = database_url or os.environ.get("DATABASE_URL", "")
    if not database_url:
        return "Error: no database URL. Pass database_url or set DATABASE_URL."

    stripped = query.strip().rstrip(";")
    first_word = stripped.split(None, 1)[0].lower() if stripped else ""
    if first_word not in ("select", "with"):
        return "Error: only SELECT/WITH queries can be explained."
    if ";" in stripped:
        return "Error: multiple statements are not allowed."

    return _run_argv(
        ["psql", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-c", f"EXPLAIN {stripped}"],
        timeout,
    )


# ── Git / deploy diff ───────────────────────────────────────────────────────


@tool
def git_recent_changes(repo_path: str, count: int = 10, timeout: int = 15) -> str:
    """Show recent commits with diffstat for a local git repository.

    Useful to correlate an incident with recent deploys/changes
    ("what changed right before the problem started?").

    Args:
        repo_path: Path to the git repository on this machine.
        count:     Number of recent commits to show (default 10, max 50).
        timeout:   Execution timeout in seconds (default 15).
    """
    path = os.path.expanduser(repo_path)
    if not os.path.isdir(path):
        return f"Error: '{repo_path}' is not a directory."
    count = max(1, min(count, 50))
    return _run_argv(
        ["git", "-C", path, "log", f"-{count}", "--date=iso", "--stat",
         "--pretty=format:%h %ad %an: %s"],
        timeout,
    )


# ── Registry ────────────────────────────────────────────────────────────────

_ALL_TOOLS = [
    run_ssh_command,
    http_get,
    run_local_command,
    query_prometheus,
    query_loki,
    query_elasticsearch,
    run_kubectl,
    run_db_explain,
    git_recent_changes,
]


def get_tools() -> list:
    """Return the enabled diagnostic tools (ENABLED_TOOLS filters; empty = all)."""
    allowed = get_settings().enabled_tool_set()
    if allowed is None:
        return list(_ALL_TOOLS)
    return [t for t in _ALL_TOOLS if t.name in allowed]
