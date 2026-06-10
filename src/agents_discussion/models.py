from langchain_openai import ChatOpenAI

from agents_discussion.config import get_settings


def create_github_model(model: str, temperature: float = 0.2) -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=model,
        api_key=settings.github_token,
        base_url=settings.github_models_base_url,
        temperature=temperature,
    )


def create_diagnostic_model() -> ChatOpenAI:
    settings = get_settings()
    return create_github_model(settings.diagnostic_model, temperature=0.2)


def create_skeptic_model() -> ChatOpenAI:
    settings = get_settings()
    return create_github_model(settings.skeptic_model, temperature=0.1)


def create_moderator_model() -> ChatOpenAI:
    settings = get_settings()
    return create_github_model(settings.moderator_model, temperature=0.0)
