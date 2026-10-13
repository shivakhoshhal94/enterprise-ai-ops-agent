import importlib
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import create_schema
from enterprise_ai_ops_agent.db.base import Base


def test_existing_metadata_and_repeat_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert create_schema.Base.metadata is Base.metadata
    assert set(Base.metadata.tables) == {
        "customers", "orders", "order_items", "products", "sellers",
        "order_payments", "category_translations",
    }
    create_all = MagicMock()
    monkeypatch.setattr(Base.metadata, "create_all", create_all)
    engine = MagicMock(spec=Engine)
    connection = engine.begin.return_value.__enter__.return_value
    for _ in range(2):
        create_schema.create_database_schema(engine)
    assert create_all.call_count == 2
    for call in create_all.call_args_list:
        assert call.kwargs == {"bind": connection, "checkfirst": True}
    assert engine.begin.return_value.__exit__.call_count == 2
    engine.dispose.assert_not_called()


def test_import_does_not_create_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    create_all = MagicMock()
    monkeypatch.setattr(Base.metadata, "create_all", create_all)
    importlib.reload(create_schema)
    create_all.assert_not_called()


def test_database_failure_is_safe_and_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    create_all = MagicMock(side_effect=OperationalError(None, None, Exception("private_password")))
    monkeypatch.setattr(Base.metadata, "create_all", create_all)
    engine = MagicMock(spec=Engine)
    with pytest.raises(create_schema.SchemaCreationError) as caught:
        create_schema.create_database_schema(engine)
    assert str(caught.value) == "Database schema creation failed."
    assert caught.value.__suppress_context__ is True
    assert engine.begin.return_value.__exit__.call_args.args[0] is OperationalError


def configure_cli(monkeypatch: pytest.MonkeyPatch, url: str | None) -> MagicMock:
    settings = Settings(_env_file=None, database_url=url)
    monkeypatch.setattr(create_schema, "Settings", lambda: settings)
    factory = MagicMock(return_value=MagicMock(spec=Engine))
    monkeypatch.setattr(create_schema, "create_db_engine", factory)
    return factory


@pytest.mark.parametrize("url", [None, "", "   "])
def test_cli_missing_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str | None) -> None:
    factory = configure_cli(monkeypatch, url)
    assert create_schema.main() == 1
    assert "set DATABASE_URL" in capsys.readouterr().err
    factory.assert_not_called()


def test_cli_configuration_failure_hides_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    factory = configure_cli(monkeypatch, "invalid")
    factory.side_effect = ValueError("private_password")
    assert create_schema.main() == 1
    output = capsys.readouterr()
    assert "configuration error" in output.err
    assert "private_password" not in output.err + output.out


@pytest.mark.parametrize("fail", [False, True])
def test_cli_result_and_engine_cleanup(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fail: bool) -> None:
    factory = configure_cli(monkeypatch, "postgresql+psycopg://localhost/test_db")
    operation = MagicMock()
    if fail:
        operation.side_effect = create_schema.SchemaCreationError("private_password")
    monkeypatch.setattr(create_schema, "create_database_schema", operation)
    assert create_schema.main() == (1 if fail else 0)
    operation.assert_called_once_with(factory.return_value)
    factory.return_value.dispose.assert_called_once_with()
    output = capsys.readouterr()
    assert "private_password" not in output.err + output.out
    assert ("creation failed" in output.err) if fail else ("schema ready" in output.out)
