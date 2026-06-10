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

# GitHub OAuth App client ID from the copilot.vim plugin — works for all
# programmatic device flows because GitHub does not enforce client/editor matching.
_CLIENT_ID = "Iv1.b507a08c87ecfe98"

_DEVICE_CODE_URL  = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_SESSION_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"

_DEFAULT_TOKEN_FILE = "~/.config/agents-discussion/copilot_token"

# Headers that impersonate the copilot.vim editor plugin.
_BASE_HEADERS: dict[str, str] = {
    "User-Agent":            "GithubCopilot/1.155.0",
    "Editor-Version":        "Neovim/0.6.1",
    "Editor-Plugin-Version": "copilot.vim/1.16.0",
    "Accept":                "application/json",
    "Content-Type":          "application/json",
}

# ── Session token cache (in-process, thread-safe) ────────────────────────────

_session_lock: threading.Lock = threading.Lock()
# Maps ghu_token → (session_token, expiry_unix_timestamp)
_session_cache: dict[str, tuple[str, float]] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ca_verify() -> str | bool:
    """Return the CA bundle path for the corporate proxy, or True (default verify)."""
    return os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True


def _token_file_path() -> Path:
    path = os.environ.get("COPILOT_TOKEN_FILE", _DEFAULT_TOKEN_FILE)
    return Path(path).expanduser()


def _parse_session_expiry(session_token: str) -> float:
    """Parse the exp= field from a semicolon-delimited Copilot session token string.

    Format: "tid=<uuid>;exp=<unix_ts>;sku=copilot_for_individuals;..."
    Returns the expiry as a float Unix timestamp, or 0.0 if not found.
    """
    for part in session_token.split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "exp":
            try:
                return float(v.strip())
            except ValueError:
                return 0.0
    return 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def get_ghu_token() -> str:
    """Return the stored ghu_... OAuth token.

    Priority:
      1. COPILOT_TOKEN environment variable
      2. get_settings().copilot_token (reads from .env)
      3. File at COPILOT_TOKEN_FILE (default: ~/.config/agents-discussion/copilot_token)
    """
    # 1. Direct env var (set at shell level or exported)
    token = os.environ.get("COPILOT_TOKEN", "").strip()
    if token:
        return token

    # 2. Via pydantic settings (reads .env file)
    try:
        from agents_discussion.config import get_settings  # noqa: PLC0415
        token = get_settings().copilot_token.strip()
        if token:
            return token
    except Exception:  # noqa: BLE001
        pass

    # 3. Token file
    path = _token_file_path()
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token

    return ""


def save_ghu_token(token: str) -> Path:
    """Persist the ghu_... token to the token file and return the path."""
    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    # Restrict permissions so only the owner can read it.
    path.chmod(0o600)
    return path


def get_session_token(ghu_token: str) -> str:
    """Exchange a ghu_... OAuth token for a short-lived Copilot session token.

    The session token is cached in memory and refreshed automatically
    60 seconds before it expires (~25-minute lifetime).
    Thread-safe.
    """
    with _session_lock:
        cached = _session_cache.get(ghu_token)
        if cached:
            session_token, expiry_ts = cached
            if expiry_ts and time.time() < expiry_ts - 60:
                return session_token

        resp = requests.get(
            _SESSION_TOKEN_URL,
            headers={
                **_BASE_HEADERS,
                "Authorization": f"token {ghu_token}",
            },
            verify=_ca_verify(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        session_token = data["token"]
        expiry_ts = _parse_session_expiry(session_token)
        _session_cache[ghu_token] = (session_token, expiry_ts)
        return session_token


def authenticate() -> str:
    """Run the GitHub OAuth device flow and return a ghu_... token.

    Prints the verification URL and user code to stdout, then polls until
    the user authorises the app in their browser.
    """
    # Step 1 — request device + user codes
    resp = requests.post(
        _DEVICE_CODE_URL,
        json={"client_id": _CLIENT_ID, "scope": "read:user"},
        headers=_BASE_HEADERS,
        verify=_ca_verify(),
        timeout=15,
    )
    resp.raise_for_status()
    device_data = resp.json()

    device_code     = device_data["device_code"]
    user_code       = device_data["user_code"]
    verification_uri = device_data["verification_uri"]
    interval        = int(device_data.get("interval", 5))

    print()
    print("  Abre en tu navegador:")
    print(f"  {verification_uri}")
    print()
    print(f"  Introduce el código: {user_code}")
    print()
    print("  Esperando autorización...", end="", flush=True)

    # Step 2 — poll until the user authorises or the code expires
    while True:
        time.sleep(interval)
        resp = requests.post(
            _ACCESS_TOKEN_URL,
            json={
                "client_id":   _CLIENT_ID,
                "device_code": device_code,
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers=_BASE_HEADERS,
            verify=_ca_verify(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if "access_token" in data:
            print(" autorizado.")
            return data["access_token"]

        error = data.get("error", "")
        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if error == "slow_down":
            interval += 5
            print(".", end="", flush=True)
            continue
        if error == "expired_token":
            raise RuntimeError("El código de dispositivo ha caducado. Vuelve a ejecutar el comando.")
        if error == "access_denied":
            raise RuntimeError("El usuario denegó el acceso.")

        raise RuntimeError(
            f"Error en la autenticación: {data.get('error_description', data)}"
        )


def main() -> None:
    """CLI entry point: run the device flow, save the token, verify the session."""
    print("─" * 55)
    print("  Autenticación GitHub Copilot")
    print("─" * 55)

    existing = get_ghu_token()
    if existing:
        print(f"  Ya existe un token almacenado ({existing[:12]}...).")
        answer = input("  ¿Quieres obtener uno nuevo? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            print("  Verificando sesión con el token existente...")
            try:
                session = get_session_token(existing)
                exp = _parse_session_expiry(session)
                mins = max(0, int((exp - time.time()) / 60)) if exp else 0
                print(f"  Sesión activa (caduca en ~{mins} min).")
            except Exception as exc:  # noqa: BLE001
                print(f"  El token existente no funciona: {exc}")
                print("  Ejecuta de nuevo para obtener uno nuevo.")
                sys.exit(1)
            return

    try:
        ghu_token = authenticate()
    except KeyboardInterrupt:
        print("\n  Cancelado.")
        sys.exit(0)

    path = save_ghu_token(ghu_token)
    print(f"  Token guardado en: {path}")
    print()

    print("  Verificando sesión...", end="", flush=True)
    try:
        session = get_session_token(ghu_token)
        exp = _parse_session_expiry(session)
        mins = max(0, int((exp - time.time()) / 60)) if exp else 25
        print(f" OK (caduca en ~{mins} min, se renovará automáticamente).")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Error al verificar la sesión: {exc}")
        sys.exit(1)

    print()
    print("  Para usar modelos Copilot, añade a tu .env:")
    print(f"  COPILOT_TOKEN={ghu_token}")
    print()
    print("  O configura un modelo con el prefijo copilot/:")
    print("  DIAGNOSTIC_MODEL=copilot/gpt-4o")
    print("  SKEPTIC_MODEL=copilot/claude-3.5-sonnet")
    print("─" * 55)
