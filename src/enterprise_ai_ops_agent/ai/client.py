"""Explicit official SDK client construction; imports perform no API calls."""

from openai import OpenAI

from enterprise_ai_ops_agent.config import Settings


def create_openai_client(settings: Settings) -> OpenAI:
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value().strip():
        raise ValueError("Set OPENAI_API_KEY before using AI functionality.")
    if not settings.openai_model.strip():
        raise ValueError("Set OPENAI_MODEL to a nonblank model name.")
    return OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=30.0, max_retries=0,
    )
