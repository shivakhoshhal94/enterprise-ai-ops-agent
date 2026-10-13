import pytest
from unittest.mock import MagicMock

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine, create_session_factory
from enterprise_ai_ops_agent.db import session as session_module


@pytest.mark.parametrize("url", [None, ""])
def test_database_configuration_is_required(url: str | None) -> None:
    settings = Settings(_env_file=None, database_url=url)
    with pytest.raises(ValueError, match="Set DATABASE_URL"):
        create_db_engine(settings)


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://localhost/example"])
def test_explicit_psycopg_driver_is_required(url: str) -> None:
    settings = Settings(_env_file=None, database_url=url)
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        create_db_engine(settings)


def test_database_name_is_required() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://localhost")
    with pytest.raises(ValueError, match="database name"):
        create_db_engine(settings)


def test_engine_and_session_creation_do_not_connect() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://localhost:1/test_database")
    engine = create_db_engine(settings)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.pool.checkedout() == 0
        with create_session_factory(engine)() as session:
            assert session.get_bind() is engine
            assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_engine_bounds_driver_connection_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(session_module, "create_engine", factory)
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://localhost/test_database")
    create_db_engine(settings)
    assert factory.call_args.kwargs["connect_args"] == {"connect_timeout": 5, "options": "-cstatement_timeout=5000"}
