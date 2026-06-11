from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _share_dir() -> Path:
    return Path.home() / ".local" / "share" / "agents-discussion"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Required only when using GitHub Models (models without "copilot/" prefix).
    # Not needed when all models are configured with "copilot/".
    github_token: str = Field("", alias="GITHUB_TOKEN")
    github_models_base_url: str = Field(
        "https://models.github.ai/inference",
        alias="GITHUB_MODELS_BASE_URL",
    )

    diagnostic_model: str = Field("copilot/gpt-4o", alias="DIAGNOSTIC_MODEL")
    skeptic_model: str = Field("copilot/claude-sonnet-4.6", alias="SKEPTIC_MODEL")
    moderator_model: str = Field("copilot/claude-sonnet-4.6", alias="MODERATOR_MODEL")

    # ── Thinking / reasoning level (per agent) ───────────────────────────
    # Only forwarded to reasoning-capable models (o1/o3/o4/gpt-5 families
    # and all Claude variants); silently ignored for others (e.g. gpt-4o).
    # Valid values: none|low|medium|high.
    diagnostic_reasoning_effort: str = Field("none", alias="DIAGNOSTIC_REASONING_EFFORT")
    skeptic_reasoning_effort: str = Field("none", alias="SKEPTIC_REASONING_EFFORT")
    moderator_reasoning_effort: str = Field("none", alias="MODERATOR_REASONING_EFFORT")

    max_rounds: int = Field(4, alias="MAX_ROUNDS", ge=1, le=10)
    confidence_threshold: float = Field(0.8, alias="CONFIDENCE_THRESHOLD", ge=0.0, le=1.0)

    # ── Prompt templates ─────────────────────────────────────────────────
    # Built-in templates ship with the package; admins can add/override
    # templates by dropping <name>.<lang>.yaml files into PROMPTS_DIR.
    prompt_template: str = Field("default", alias="PROMPT_TEMPLATE")
    prompt_language: str = Field("es", alias="PROMPT_LANGUAGE")
    prompts_dir: Path = Field(
        default_factory=lambda: _share_dir() / "prompts",
        alias="PROMPTS_DIR",
    )

    # ── Tool configuration ──────────────────────────────────────────────
    tools_enabled: bool = Field(True, alias="TOOLS_ENABLED")
    # Comma-separated tool names to expose to agents. Empty = all tools.
    enabled_tools: str = Field("", alias="ENABLED_TOOLS")
    max_tool_calls_per_agent: int = Field(8, alias="MAX_TOOL_CALLS_PER_AGENT", ge=1, le=50)
    max_consecutive_errors: int = Field(3, alias="MAX_CONSECUTIVE_ERRORS", ge=1, le=10)

    # ── Tool approval gating (web runs only) ─────────────────────────────
    tool_approval_required: bool = Field(True, alias="TOOL_APPROVAL_REQUIRED")
    approval_required_tools: str = Field(
        "run_ssh_command,run_local_command,run_kubectl,run_db_explain",
        alias="APPROVAL_REQUIRED_TOOLS",
    )
    approval_timeout_seconds: int = Field(300, alias="APPROVAL_TIMEOUT_SECONDS", ge=10, le=3600)
    comment_timeout_seconds: int = Field(600, alias="COMMENT_TIMEOUT_SECONDS", ge=10, le=7200)

    # SSH defaults (agents can override per-call)
    ssh_default_user: str = Field("", alias="SSH_DEFAULT_USER")
    ssh_key_path: str = Field("", alias="SSH_KEY_PATH")
    ssh_connect_timeout: int = Field(15, alias="SSH_CONNECT_TIMEOUT", ge=1, le=120)

    # ── GitHub Copilot provider ─────────────────────────────────────────
    # Long-lived OAuth token (ghu_...) obtained via: agents-discuss-copilot-auth
    # Session tokens (~25 min) are managed automatically by auth_copilot.py.
    copilot_token: str = Field("", alias="COPILOT_TOKEN")

    # ── Persistence ──────────────────────────────────────────────────────
    # Directory where completed run JSON files are stored. The tool audit
    # log (audit.jsonl) lives in the same directory.
    data_dir: Path = Field(
        default_factory=lambda: _share_dir() / "runs",
        alias="DATA_DIR",
    )

    # ── Web server ───────────────────────────────────────────────────────
    web_host: str = Field("127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(8000, alias="WEB_PORT", ge=1, le=65535)

    def approval_tool_set(self) -> set[str]:
        return {t.strip() for t in self.approval_required_tools.split(",") if t.strip()}

    def enabled_tool_set(self) -> set[str] | None:
        """Names of tools to expose, or None meaning 'all'."""
        names = {t.strip() for t in self.enabled_tools.split(",") if t.strip()}
        return names or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
