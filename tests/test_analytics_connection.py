from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from enterprise_ai_ops_agent.analytics import executor
from enterprise_ai_ops_agent.analytics.models import AnalyticalQueryError, AnalyticalResult
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import session
from enterprise_ai_ops_agent.main import create_app

ADMIN_URL = "postgresql+psycopg://admin:admin_secret@localhost/enterprise_ai_ops_agent"
READER_URL = "postgresql+psycopg://enterprise_ai_ops_reader:reader_secret@localhost/enterprise_ai_ops_agent"


def test_analytics_optional_for_settings_health_and_admin(monkeypatch):
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=None, openai_api_key=None)
    assert settings.analytics_database_url is None
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
    factory = MagicMock()
    monkeypatch.setattr(session, "create_engine", factory)
    session.create_db_engine(settings)
    assert factory.call_args.args[0].username == "admin"


@pytest.mark.parametrize("url", [None, "", "sqlite://", ADMIN_URL, "not a URL",
    "postgresql+psycopg://enterprise_ai_ops_reader@localhost/enterprise_ai_ops_agent",
    "postgresql+psycopg://enterprise_ai_ops_reader:secret@localhost",
])
def test_analytics_configuration_fails_closed(url, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(session, "create_engine", factory)
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=url)
    with pytest.raises(ValueError) as error:
        session.create_analytics_engine(settings)
    assert "admin_secret" not in str(error.value) and "reader_secret" not in str(error.value)
    factory.assert_not_called()


def test_dedicated_factory_is_lazy_and_secret_safe():
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL)
    assert "reader_secret" not in repr(settings)
    assert "reader_secret" not in settings.model_dump_json()
    engine = session.create_analytics_engine(settings)
    try:
        assert engine.url.username == "enterprise_ai_ops_reader"
        assert engine.pool.checkedout() == 0
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_configured_executor_uses_reader_and_disposes(monkeypatch):
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL)
    engine = MagicMock()
    factory = MagicMock(return_value=engine)
    execute = MagicMock(return_value=AnalyticalResult(columns=["n"], rows=[[1]], returned_row_count=1, truncated=False))
    monkeypatch.setattr(executor, "create_analytics_engine", factory)
    monkeypatch.setattr(executor, "execute_analytical_query", execute)
    query = validate_analytical_sql("SELECT 1")
    assert executor.execute_configured_analytical_query(settings, query).rows == [[1]]
    factory.assert_called_once_with(settings)
    execute.assert_called_once_with(engine, query, max_rows=100, timeout_ms=3000)
    engine.dispose.assert_called_once()


def test_configured_executor_no_admin_fallback():
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=None)
    with pytest.raises(AnalyticalQueryError, match="ANALYTICS_DATABASE_URL") as error:
        executor.execute_configured_analytical_query(settings, validate_analytical_sql("SELECT 1"))
    assert "admin_secret" not in str(error.value)


def test_configured_executor_disposes_on_failure(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(executor, "create_analytics_engine", lambda settings: engine)
    monkeypatch.setattr(executor, "execute_analytical_query", MagicMock(side_effect=AnalyticalQueryError("failed")))
    with pytest.raises(AnalyticalQueryError):
        executor.execute_configured_analytical_query(Settings(_env_file=None), validate_analytical_sql("SELECT 1"))
    engine.dispose.assert_called_once()
