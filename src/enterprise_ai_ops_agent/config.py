"""Validated application configuration loaded from the environment."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="Enterprise AI Operations Agent", min_length=1)
    environment: str = Field(default="development", min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr | None = None
    analytics_database_url: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    approval_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-4.1-mini", min_length=1)
    openai_embedding_model: str = Field(default="text-embedding-3-small", min_length=1)
    openai_embedding_dimensions: int = Field(default=1536, ge=1536, le=1536)
    knowledge_database_url: SecretStr | None = None
