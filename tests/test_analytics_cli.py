from unittest.mock import MagicMock

from enterprise_ai_ops_agent.analytics import cli
from enterprise_ai_ops_agent.analytics.models import AnalyticalQueryError, AnalyticalResult
from enterprise_ai_ops_agent.config import Settings


def test_cli_success(monkeypatch, capsys):
    engine = MagicMock()
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "create_analytics_engine", lambda settings: engine)
    execute = MagicMock(return_value=AnalyticalResult(columns=["n"], rows=[[1]], returned_row_count=1, truncated=False))
    monkeypatch.setattr(cli, "execute_analytical_query", execute)
    assert cli.main(["SELECT 1 AS n"]) == 0
    assert '"returned_row_count":1' in capsys.readouterr().out
    engine.dispose.assert_called_once()


def test_cli_rejection_before_engine(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    factory = MagicMock()
    monkeypatch.setattr(cli, "create_analytics_engine", factory)
    assert cli.main(["DELETE FROM orders"]) == 1
    assert "SQL rejected" in capsys.readouterr().err
    factory.assert_not_called()


def test_cli_configuration_failure_safe(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, analytics_database_url=None))
    assert cli.main(["SELECT 1"]) == 1
    assert capsys.readouterr().err == "Analytics configuration error: check ANALYTICS_DATABASE_URL.\n"


def test_cli_database_failure_safe(monkeypatch, capsys):
    engine = MagicMock()
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "create_analytics_engine", lambda settings: engine)
    monkeypatch.setattr(cli, "execute_analytical_query", MagicMock(side_effect=AnalyticalQueryError("Analytical query failed.")))
    assert cli.main(["SELECT 1"]) == 1
    assert capsys.readouterr().err == "Analytical query failed.\n"
    engine.dispose.assert_called_once()
