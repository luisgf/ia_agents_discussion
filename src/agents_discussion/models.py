# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import re

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from agents_discussion.config import get_settings


# ── Reasoning effort (thinking level) ────────────────────────────────────────
#
# Only OpenAI reasoning families accept the `reasoning_effort` parameter over
# the OpenAI-compatible endpoints. Non-reasoning models (e.g. gpt-4o) and
# Claude reject it, so we both normalize the requested value and gate it
# behind an allowlist matched on name segments to avoid false positives
# (e.g. "gpt-4o" must NOT match the "o4" reasoning family).

_VALID_EFFORTS = ("low", "medium", "high")
_REASONING_MARKERS = ("o1", "o3", "o4")  # exact name segments for OpenAI families
_CLAUDE_FAMILIES = frozenset({"sonnet", "opus"})  # families that may support thinking


def _normalize_effort(value: str | None) -> str | None:
    """Return a valid effort level or None (meaning: do not send the param)."""
    if not value:
        return None
    v = value.strip().lower()
    return v if v in _VALID_EFFORTS else None


def _segments(model: str) -> list[str]:
    """Split a model name into comparable segments (copilot/ prefix stripped)."""
    return re.split(r"[/\-.]", model.removeprefix("copilot/").lower())


def _is_openai_reasoning_family(model: str) -> bool:
    """True for OpenAI o-series and gpt-5 families."""
    segments = _segments(model)
    if any(seg in _REASONING_MARKERS for seg in segments):
        return True
    name = model.removeprefix("copilot/").lower()
    return any(seg.startswith("gpt-5") or seg == "gpt5" for seg in segments) or "gpt-5" in name


def supports_temperature(model: str, effort: str | None = None) -> bool:
    """True when the model accepts a custom `temperature`.

    - OpenAI o-series / gpt-5 reject any non-default temperature outright.
    - Claude requires the default temperature when extended thinking is on,
      so it is dropped whenever a reasoning effort is actually sent.
    """
    if _is_openai_reasoning_family(model):
        return False
    if effort is not None and "claude" in _segments(model):
        return False
    return True


def _claude_supports_reasoning(segments: list[str]) -> bool:
    """True for Claude variants that support extended thinking via reasoning_effort.

    Supported patterns (after splitting on '/', '-', '.'):
      claude-{family}-4.x   →  v4 style: sonnet/opus + major version >= 4
      claude-3.7-{family}   →  the 3.7 series (first Claude with extended thinking)

    Not supported: all haiku variants, claude-3.5-* and any other 3.x series.
    """
    if "haiku" in segments:
        return False
    try:
        tail = segments[segments.index("claude") + 1 :]
    except ValueError:
        return False
    # v4+ style: claude-{family}-{major}.{minor}  e.g. claude-sonnet-4-5
    if tail and tail[0] in _CLAUDE_FAMILIES:
        return len(tail) > 1 and tail[1].isdigit() and int(tail[1]) >= 4
    # v3.7 style: claude-3.7-{family}  →  segments [...,'3','7',...]
    if len(tail) >= 2 and tail[0] == "3" and tail[1] == "7":
        return True
    return False


def supports_reasoning(model: str) -> bool:
    """True when the model accepts the `reasoning_effort` parameter.

    - OpenAI reasoning families (o1/o3/o4) and gpt-5: always accepted.
    - Claude: only sonnet/opus v4+ and 3.7-sonnet; haiku and 3.5/3.x excluded.
    """
    segments = _segments(model)
    if "claude" in segments:
        return _claude_supports_reasoning(segments)
    return _is_openai_reasoning_family(model)


def requires_responses_api(model: str) -> bool:
    """True for models only reachable through the OpenAI Responses API (/responses).

    GitHub Copilot exposes gpt-5.5 (and its variants, e.g. gpt-5.5-codex)
    exclusively on /responses; hitting /chat/completions returns
    HTTP 400 `unsupported_api_for_model`. langchain-openai switches to that
    endpoint when `use_responses_api=True`.
    """
    name = model.removeprefix("copilot/").lower()
    return "gpt-5.5" in name or "gpt-5-5" in name


def _http_client() -> httpx.Client | None:
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle:
        return httpx.Client(verify=ca_bundle)
    return None


# ── GitHub Models (ChatOpenAI → models.github.ai) ────────────────────────────


def _create_github_models_model(
    model: str, temperature: float | None, reasoning_effort: str | None = None
) -> ChatOpenAI:
    settings = get_settings()
    if not settings.github_token:
        raise ValueError(
            "GITHUB_TOKEN is not configured. "
            "Add GITHUB_TOKEN=<your_pat> to the .env file, "
            "or use the 'copilot/' prefix in the model name to use GitHub Copilot."
        )
    kwargs: dict = {}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if temperature is not None:
        kwargs["temperature"] = temperature
    if requires_responses_api(model):
        kwargs["use_responses_api"] = True
    return ChatOpenAI(
        model=model,
        api_key=settings.github_token,
        base_url=settings.github_models_base_url,
        http_client=_http_client(),
        **kwargs,
    )


# ── GitHub Copilot (ChatOpenAI → api.githubcopilot.com) ──────────────────────
#
# langchain-github-copilot pins langchain-core<0.4.0, incompatible with
# langchain-openai and langgraph (both require >=1.x).  We replicate the
# same behaviour — correct endpoint + impersonation headers — directly on
# ChatOpenAI, which avoids the conflict entirely.

_COPILOT_BASE_URL = "https://api.githubcopilot.com"

_COPILOT_HEADERS: dict[str, str] = {
    "User-Agent": "GithubCopilot/1.155.0",
    "Editor-Version": "Neovim/0.6.1",
    "Editor-Plugin-Version": "copilot.vim/1.16.0",
    "OpenAI-Intent": "conversation-panel",
    "OpenAI-Organization": "github-copilot",
    "Copilot-Integration-Id": "vscode-chat",
}


def _create_copilot_model(model: str, temperature: float | None, reasoning_effort: str | None = None) -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at the Copilot inference endpoint.

    Authentication is a two-stage flow:
      ghu_... OAuth token  →  short-lived session token (~25 min, auto-refreshed)
    Both stages are handled by auth_copilot.get_session_token().
    """
    from agents_discussion.auth_copilot import get_ghu_token, get_session_token  # noqa: PLC0415

    ghu_token = get_ghu_token()
    if not ghu_token:
        raise ValueError(
            "GitHub Copilot token (ghu_...) not found. "
            "Run: agents-discuss-copilot-auth  "
            "or set COPILOT_TOKEN in the environment."
        )

    session_token = get_session_token(ghu_token)
    kwargs: dict = {}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if temperature is not None:
        kwargs["temperature"] = temperature
    if requires_responses_api(model):
        kwargs["use_responses_api"] = True
    return ChatOpenAI(
        model=model,
        api_key=session_token,
        base_url=_COPILOT_BASE_URL,
        default_headers=_COPILOT_HEADERS,
        http_client=_http_client(),
        **kwargs,
    )


# ── Router ────────────────────────────────────────────────────────────────────


def create_github_model(
    model: str,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
) -> BaseChatModel:
    """Return the appropriate chat model based on the model name prefix.

    Prefixes:
      copilot/<name>  →  ChatOpenAI via api.githubcopilot.com
      anything else   →  ChatOpenAI via models.github.ai

    `reasoning_effort` (low|medium|high) is only forwarded when the value is
    valid AND the model belongs to a reasoning-capable family; otherwise it is
    silently dropped so non-reasoning models keep working unchanged.

    `temperature` is dropped for models that reject it (OpenAI o-series/gpt-5,
    and Claude when extended thinking is enabled) so the request doesn't 400.
    """
    effort = _normalize_effort(reasoning_effort)
    if effort is not None and not supports_reasoning(model):
        effort = None
    resolved_temperature: float | None = temperature
    if not supports_temperature(model, effort):
        resolved_temperature = None
    if model.startswith("copilot/"):
        return _create_copilot_model(model.removeprefix("copilot/"), resolved_temperature, effort)
    return _create_github_models_model(model, resolved_temperature, effort)


def create_diagnostic_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(
        settings.diagnostic_model,
        temperature=0.2,
        reasoning_effort=settings.diagnostic_reasoning_effort,
    )


def create_skeptic_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(
        settings.skeptic_model,
        temperature=0.1,
        reasoning_effort=settings.skeptic_reasoning_effort,
    )


def create_moderator_model() -> BaseChatModel:
    settings = get_settings()
    return create_github_model(
        settings.moderator_model,
        temperature=0.0,
        reasoning_effort=settings.moderator_reasoning_effort,
    )
