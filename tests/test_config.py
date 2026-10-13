import os

import pytest
from pydantic import ValidationError

from enterprise_ai_ops_agent.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper() in {"APP_NAME", "ENVIRONMENT", "LOG_LEVEL", "DATABASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"}:
            monkeypatch.delenv(name)


def test_default_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Enterprise AI Operations Agent"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.database_url is None
    assert settings.openai_api_key is None
    assert settings.openai_model == "gpt-4.1-mini"


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "Configured service")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings(_env_file=None)

    assert settings.app_name == "Configured service"
    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "INVALID")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_url_override_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://test_user:test_password@localhost/test_database"
    monkeypatch.setenv("DATABASE_URL", url)

    settings = Settings(_env_file=None)

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == url
    assert "test_password" not in repr(settings)
    assert "test_password" not in settings.model_dump_json()
