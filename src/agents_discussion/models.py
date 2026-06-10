import os

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from agents_discussion.config import get_settings


def _http_client() -> httpx.Client | None:
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle:
        return httpx.Client(verify=ca_bundle)
    return None


# ── GitHub Models (ChatOpenAI → models.github.ai) ────────────────────────────

def _create_github_models_model(model: str, temperature: float) -> ChatOpenAI:
    settings = get_settings()
    if not settings.github_token:
        raise ValueError(
            "GITHUB_TOKEN no está configurado. "
            "Añade GITHUB_TOKEN=<tu_pat> en el fichero .env, "
            "o usa el prefijo 'copilot/' en el nombre del modelo para usar GitHub Copilot."
        )
    return ChatOpenAI(
        model=model,
        api_key=settings.github_token,
        base_url=settings.github_models_base_url,
        temperature=temperature,
        http_client=_http_client(),
    )


# ── GitHub Copilot (ChatOpenAI → api.githubcopilot.com) ──────────────────────
#
# langchain-github-copilot pins langchain-core<0.4.0, incompatible with
# langchain-openai and langgraph (both require >=1.x).  We replicate the
# same behaviour — correct endpoint + impersonation headers — directly on
# ChatOpenAI, which avoids the conflict entirely.

_COPILOT_BASE_URL = "https://api.githubcopilot.com"

_COPILOT_HEADERS: dict[str, str] = {
    "User-Agent":              "GithubCopilot/1.155.0",
    "Editor-Version":          "Neovim/0.6.1",
    "Editor-Plugin-Version":   "copilot.vim/1.16.0",
    "OpenAI-Intent":           "conversation-panel",
    "OpenAI-Organization":     "github-copilot",
    "Copilot-Integration-Id":  "vscode-chat",
}


def _create_copilot_model(model: str, temperature: float) -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at the Copilot inference endpoint.

    Authentication is a two-stage flow:
      ghu_... OAuth token  →  short-lived session token (~25 min, auto-refreshed)
    Both stages are handled by auth_copilot.get_session_token().
    """
    from agents_discussion.auth_copilot import get_ghu_token, get_session_token  # noqa: PLC0415

    ghu_token = get_ghu_token()
    if not ghu_token:
        raise ValueError(
            "No se encontró el token de GitHub Copilot (ghu_...). "
            "Ejecuta: agents-discuss-copilot-auth  "
            "o establece COPILOT_TOKEN en el entorno."
        )

    session_token = get_session_token(ghu_token)
    return ChatOpenAI(
        model=model,
        api_key=session_token,
        base_url=_COPILOT_BASE_URL,
        temperature=temperature,
        default_headers=_COPILOT_HEADERS,
        http_client=_http_client(),
    )


# ── Router ────────────────────────────────────────────────────────────────────

def create_github_model(model: str, temperature: float = 0.2) -> BaseChatModel:
    """Return the appropriate chat model based on the model name prefix.

    Prefixes:
      copilot/<name>  →  ChatOpenAI via api.githubcopilot.com
      anything else   →  ChatOpenAI via models.github.ai
    """
    if model.startswith("copilot/"):
        return _create_copilot_model(model.removeprefix("copilot/"), temperature)
    return _create_github_models_model(model, temperature)


def create_diagnostic_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(settings.diagnostic_model, temperature=0.2)


def create_skeptic_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(settings.skeptic_model, temperature=0.1)


def create_moderator_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(settings.moderator_model, temperature=0.0)
