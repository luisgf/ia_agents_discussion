from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    github_token: str = Field(..., alias="GITHUB_TOKEN")
    github_models_base_url: str = Field(
        "https://models.github.ai/inference",
        alias="GITHUB_MODELS_BASE_URL",
    )

    diagnostic_model: str = Field("openai/gpt-4.1", alias="DIAGNOSTIC_MODEL")
    skeptic_model: str = Field("anthropic/claude-3.5-sonnet", alias="SKEPTIC_MODEL")
    moderator_model: str = Field("google/gemini-2.0-flash", alias="MODERATOR_MODEL")

    max_rounds: int = Field(4, alias="MAX_ROUNDS", ge=1, le=10)
    confidence_threshold: float = Field(0.8, alias="CONFIDENCE_THRESHOLD", ge=0.0, le=1.0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
