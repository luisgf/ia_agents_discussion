"""GitHub Copilot OAuth authentication helpers.

Two-stage token flow
--------------------
1. OAuth device flow  →  ghu_... token  (long-lived, never expires automatically)
2. Session token exchange  →  session token  (valid ~25 min, auto-refreshed here)

CLI usage
---------
Run the device flow once and save the ghu_... token:

    agents-discuss-copilot-auth

After that, the session token is refreshed automatically on every model creation.
The ghu_... token is stored in ~/.config/agents-discussion/copilot_token
(override with the COPILOT_TOKEN_FILE env var) and can also be set directly
via the COPILOT_TOKEN env var.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import requests

# ── Constants ────────────────────────────────────────────────────────────────

_CLIENT_ID = "Iv1.b507a08c87ecfe98"

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_SESSION_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"

_DEFAULT_TOKEN_FILE = "~/.config/agents-discussion/copilot_token"

_BASE_HEADERS: dict[str, str] = {
    "User-Agent":            "GithubCopilot/1.155.0",
    "Editor-Version":        "Neovim/0.6.1",
    "Editor-Plugin-Version": "copilot.vim/1.16.0",
    "Accept":                "application/json",
    "Content-Type":          "application/json",
}

# ── Session token cache ──────────────────────────────────────────────────────

_session_lock: threading.Lock = threading.Lock()
_session_cache: dict[str, tuple[str, float]] = {}

# ── Device flow state (for web UI polling) ───────────────────────────────────

_device_flow_lock: threading.Lock = threading.Lock()
_device_flow_state: dict[str, dict] = {}


def _ca_verify() -> str | bool:
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


def _token_file_path() -> Path:
    path = os.environ.get("COPILOT_TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    return Path(path).expanduser()


def _parse_session_expiry(session_token: str) -> float:
    for part in session_token.split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "exp":
            try:
                return float(v.strip())
            except ValueError:
                return 0.0
    return 0.0


def get_ghu_token() -> str:
    token = os.environ.get("COPILOT_TOKEN", "").strip()
    if token:
        return token
    try:
        from agents_discussion.config import get_settings
        token = get_settings().copilot_token.strip()
        if token:
            return token
    except Exception:
        pass
    path = _token_file_path()
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    return ""


def save_ghu_token(token: str) -> Path:
    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)
    return path


def get_session_token(ghu_token: str) -> str:
    with _session_lock:
        cached = _session_cache.get(ghu_token)
        if cached:
            session_token, expiry_ts = cached
            if expiry_ts and time.time() < expiry_ts - 60:
                return session_token

        resp = requests.get(
            _SESSION_TOKEN_URL,
            headers={**_BASE_HEADERS, "Authorization": f"token {ghu_token}"},
            verify=_ca_verify(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        session_token = data["token"]
        expiry_ts = _parse_session_expiry(session_token)
        _session_cache[ghu_token] = (session_token, expiry_ts)
        return session_token


def get_auth_status() -> dict:
    result: dict = {
        "copilot_configured": False,
        "copilot_token_preview": None,
        "copilot_session_valid": False,
        "copilot_session_expires_in_seconds": None,
        "github_models_configured": False,
        "github_models_token_preview": None,
        "last_error": None,
    }
    ghu = get_ghu_token()
    if ghu:
        result["copilot_configured"] = True
        result["copilot_token_preview"] = ghu[:12] + "..."
        try:
            session = get_session_token(ghu)
            exp = _parse_session_expiry(session)
            if exp:
                remaining = int(exp - time.time())
                result["copilot_session_valid"] = remaining > 0
                result["copilot_session_expires_in_seconds"] = max(0, remaining)
            else:
                result["copilot_session_valid"] = True
                result["copilot_session_expires_in_seconds"] = 1500
        except requests.HTTPError as exc:
            result["copilot_session_valid"] = False
            result["last_error"] = f"Session token error: {exc}"
        except Exception as exc:
            result["copilot_session_valid"] = False
            result["last_error"] = str(exc)

    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not gh_token:
        try:
            from agents_discussion.config import get_settings
            gh_token = get_settings().github_token.strip()
        except Exception:
            pass
    if gh_token:
        result["github_models_configured"] = True
        result["github_models_token_preview"] = gh_token[:12] + "..."
    return result


def start_device_flow() -> dict:
    resp = requests.post(
        _DEVICE_CODE_URL,
        json={"client_id": _CLIENT_ID, "scope": "read:user"},
        headers=_BASE_HEADERS,
        verify=_ca_verify(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    device_code = data["device_code"]
    flow_info = {
        "device_code": device_code,
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        "interval": int(data.get("interval", 5)),
        "expires_in": int(data.get("expires_in", 900)),
        "status": "pending",
        "ghu_token": None,
        "last_error": None,
    }
    with _device_flow_lock:
        _device_flow_state[device_code] = flow_info
    return flow_info


def check_device_flow(device_code: str) -> dict:
    with _device_flow_lock:
        flow = _device_flow_state.get(device_code)
    if not flow:
        return {"status": "error", "last_error": "Unknown device code"}

    if flow["status"] in ("authorized", "denied", "expired", "error"):
        return flow

    resp = requests.post(
        _ACCESS_TOKEN_URL,
        json={
            "client_id": _CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        headers=_BASE_HEADERS,
        verify=_ca_verify(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "access_token" in data:
        token = data["access_token"]
        flow["status"] = "authorized"
        flow["ghu_token"] = token
        save_ghu_token(token)
        with _session_lock:
            for key in list(_session_cache.keys()):
                _session_cache.pop(key, None)
        return flow

    error = data.get("error", "")
    if error == "authorization_pending":
        return flow
    if error == "slow_down":
        flow["interval"] += 5
        return flow
    if error == "expired_token":
        flow["status"] = "expired"
        flow["last_error"] = "El código de dispositivo ha caducado."
        return flow
    if error == "access_denied":
        flow["status"] = "denied"
        flow["last_error"] = "El usuario denegó el acceso."
        return flow

    flow["status"] = "error"
    flow["last_error"] = data.get("error_description", str(data))
    return flow


def authenticate() -> str:
    flow = start_device_flow()
    device_code = flow["device_code"]
    user_code = flow["user_code"]
    verification_uri = flow["verification_uri"]
    interval = flow["interval"]

    print()
    print("  Abre en tu navegador:")
    print(f"  {verification_uri}")
    print()
    print(f"  Introduce el código: {user_code}")
    print()
    print("  Esperando autorización...", end="", flush=True)

    while True:
        time.sleep(interval)
        result = check_device_flow(device_code)
        status = result["status"]

        if status == "authorized":
            print(" autorizado.")
            return result["ghu_token"]
        if status == "pending":
            print(".", end="", flush=True)
            continue
        if status == "expired":
            raise RuntimeError("El código de dispositivo ha caducado.")
        if status == "denied":
            raise RuntimeError("El usuario denegó el acceso.")
        raise RuntimeError(f"Error: {result.get('last_error', 'unknown')}")


def main() -> None:
    print("─" * 55)
    print("  Autenticación GitHub Copilot")
    print("─" * 55)

    existing = get_ghu_token()
    if existing:
        print(f"  Ya existe un token ({existing[:12]}...).")
        answer = input("  ¿Nuevo? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            try:
                session = get_session_token(existing)
                exp = _parse_session_expiry(session)
                mins = max(0, int((exp - time.time()) / 60)) if exp else 0
                print(f"  Sesión OK (caduca en ~{mins} min).")
            except Exception as exc:
                print(f"  Error: {exc}")
                sys.exit(1)
            return

    try:
        ghu_token = authenticate()
    except KeyboardInterrupt:
        print("\n  Cancelado.")
        sys.exit(0)

    path = save_ghu_token(ghu_token)
    print(f"  Token guardado en: {path}")

    try:
        session = get_session_token(ghu_token)
        exp = _parse_session_expiry(session)
        mins = max(0, int((exp - time.time()) / 60)) if exp else 25
        print(f"  Sesión OK (caduca en ~{mins} min, auto-renovable).")
    except Exception as exc:
        print(f"  Error: {exc}")
        sys.exit(1)

    print()
    print("  COPILOT_TOKEN:", ghu_token)
    print("  DIAGNOSTIC_MODEL=copilot/gpt-4o")
    print("─" * 55)


if __name__ == "__main__":
    main()
