"""
Diagnostic tools available to all agents during the debate.

Tools:
  - run_ssh_command   : execute a command on a remote host via SSH
  - http_get          : HTTP GET request (health-checks, internal APIs)
  - run_local_command : execute a read-only shell command locally
"""
from __future__ import annotations

import getpass
import os
import subprocess
import time
from typing import Optional

import httpx
import paramiko
from langchain_core.tools import tool

# Maximum characters kept from any tool output (prevents prompt bloat)
_MAX_OUT = 4_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUT:
        return text
    return text[:_MAX_OUT] + f"\n... [truncated — {len(text)} chars total]"


def _ca_bundle() -> str | bool:
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


# ── SSH ─────────────────────────────────────────────────────────────────────


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
        key_path: Path to the private key file. Falls back to SSH_KEY_PATH env var.
        port:     SSH port (default 22).
        timeout:  Connection + execution timeout in seconds (default 15).
    """
    user = user or os.environ.get("SSH_DEFAULT_USER") or getpass.getuser()
    key_path = key_path or os.environ.get("SSH_KEY_PATH", "")

    if not host:
        return "Error: 'host' is required."

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # When key_path is empty let paramiko auto-discover ~/.ssh/id_* and the SSH agent.
    key_filename = os.path.expanduser(key_path) if key_path else None

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
        return _truncate(f"# {user}@{host} $ {command}  ({elapsed} ms)\n{result}")

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


# ── Registry ────────────────────────────────────────────────────────────────


def get_tools() -> list:
    """Return the list of all available diagnostic tools."""
    return [run_ssh_command, http_get, run_local_command]
