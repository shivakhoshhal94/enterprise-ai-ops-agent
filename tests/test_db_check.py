from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import check


def fake_engine(value: int = 1) -> MagicMock:
    engine = MagicMock(spec=Engine)
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = value
    return engine


def test_success_runs_only_select_one_and_closes_connection() -> None:
    engine = fake_engine()
    assert check.check_database_connection(engine) is True
    engine.connect.assert_called_once_with()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.assert_called_once()
    assert str(connection.execute.call_args.args[0]) == "SELECT 1"
    engine.connect.return_value.__exit__.assert_called_once()
    engine.dispose.assert_not_called()  # The engine belongs to the caller.


@pytest.mark.parametrize("stage", ["connect", "execute"])
def test_connection_errors_are_translated_without_credentials(stage: str) -> None:
    engine = fake_engine()
    error = OperationalError(None, None, Exception("password=private_test_password"))
    if stage == "connect":
        engine.connect.side_effect = error
    else:
        engine.connect.return_value.__enter__.return_value.execute.side_effect = error
    with pytest.raises(check.DatabaseConnectionError) as caught:
        check.check_database_connection(engine)
    assert str(caught.value) == "Database connection check failed."
    assert "private_test_password" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_unexpected_result_is_failure() -> None:
    with pytest.raises(check.DatabaseConnectionError, match="unexpected result"):
        check.check_database_connection(fake_engine(0))


def configure_cli(monkeypatch: pytest.MonkeyPatch, url: str | None) -> MagicMock:
    settings = Settings(_env_file=None, database_url=url)
    monkeypatch.setattr(check, "Settings", lambda: settings)
    factory = MagicMock(return_value=fake_engine())
    monkeypatch.setattr(check, "create_db_engine", factory)
    return factory


@pytest.mark.parametrize("url", [None, "", "   "])
def test_cli_missing_url_does_not_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str | None,
) -> None:
    factory = configure_cli(monkeypatch, url)
    assert check.main() == 1
    output = capsys.readouterr()
    assert "set DATABASE_URL" in output.err
    assert output.out == ""
    factory.assert_not_called()


def test_cli_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    factory = configure_cli(monkeypatch, "postgresql+psycopg://localhost/test_database")
    assert check.main() == 0
    assert capsys.readouterr().out == "Database connection successful (SELECT 1).\n"
    factory.return_value.dispose.assert_called_once_with()


def test_cli_failure_hides_driver_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    factory = configure_cli(monkeypatch, "postgresql+psycopg://user:private_test_password@localhost/db")
    factory.return_value.connect.side_effect = OperationalError(
        None, None, Exception("postgresql+psycopg://user:private_test_password@localhost/db"),
    )
    assert check.main() == 1
    output = capsys.readouterr()
    assert "Database connection failed" in output.err
    assert "private_test_password" not in output.err + output.out
    factory.return_value.dispose.assert_called_once_with()


def test_cli_invalid_configuration_hides_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    factory = configure_cli(monkeypatch, "invalid_url")
    factory.side_effect = ValueError("private_test_password")
    assert check.main() == 1
    output = capsys.readouterr()
    assert "Database configuration error" in output.err
    assert "private_test_password" not in output.err + output.out
