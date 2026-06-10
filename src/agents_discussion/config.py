from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    max_rounds: int = Field(4, alias="MAX_ROUNDS", ge=1, le=10)
    confidence_threshold: float = Field(0.8, alias="CONFIDENCE_THRESHOLD", ge=0.0, le=1.0)

    # ── Tool configuration ──────────────────────────────────────────────
    tools_enabled: bool = Field(True, alias="TOOLS_ENABLED")
    max_tool_calls_per_agent: int = Field(8, alias="MAX_TOOL_CALLS_PER_AGENT", ge=1, le=30)
    max_consecutive_errors: int = Field(3, alias="MAX_CONSECUTIVE_ERRORS", ge=1, le=10)

    # SSH defaults (agents can override per-call)
    ssh_default_user: str = Field("", alias="SSH_DEFAULT_USER")
    ssh_key_path: str = Field("", alias="SSH_KEY_PATH")
    ssh_connect_timeout: int = Field(15, alias="SSH_CONNECT_TIMEOUT", ge=1, le=120)

    # ── GitHub Copilot provider ─────────────────────────────────────────
    # Long-lived OAuth token (ghu_...) obtained via: agents-discuss-copilot-auth
    # Session tokens (~25 min) are managed automatically by auth_copilot.py.
    copilot_token: str = Field("", alias="COPILOT_TOKEN")

    # ── Persistence ──────────────────────────────────────────────────────
    # Directory where completed run JSON files are stored.
    data_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "agents-discussion" / "runs",
        alias="DATA_DIR",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
