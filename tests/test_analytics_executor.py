from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from enterprise_ai_ops_agent.analytics.executor import execute_analytical_query
from enterprise_ai_ops_agent.analytics.models import AnalyticalQueryError, AnalyticalQueryTimeout, ValidatedQuery
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql


def fake_engine(rows=(), columns=("value",)):
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    connection = engine.connect.return_value.execution_options.return_value.__enter__.return_value
    result = MagicMock()
    result.keys.return_value = columns
    result.fetchmany.return_value = rows
    connection.exec_driver_sql.return_value = result
    return engine, connection, result


@pytest.mark.parametrize("rows,truncated", [([], False), ([(1,)], False), ([(1,), (2,)], False), ([(1,), (2,), (3,)], True)])
def test_result_bounds_and_transaction(rows, truncated):
    engine, connection, result = fake_engine(rows)
    output = execute_analytical_query(engine, validate_analytical_sql("SELECT 1 AS value"), max_rows=2)
    assert output.columns == ["value"]
    assert output.rows == [list(row) for row in rows[:2]]
    assert output.returned_row_count == min(2, len(rows))
    assert output.truncated is truncated
    engine.connect.return_value.execution_options.assert_called_once_with(postgresql_readonly=True)
    sql = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
    assert sql[:3] == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '3000ms'", "SET LOCAL search_path = pg_catalog"]
    assert sql[3].endswith("LIMIT 3")
    result.fetchmany.assert_called_once_with(3)
    result.close.assert_called_once()
    connection.begin.return_value.rollback.assert_called_once()
    connection.commit.assert_not_called()


def test_decimal_and_null_preserved():
    engine, _, _ = fake_engine([(Decimal("298.90"), None)], ("money", "missing"))
    output = execute_analytical_query(engine, validate_analytical_sql("SELECT payment_value FROM order_payments"))
    assert isinstance(output.rows[0][0], Decimal)
    assert output.rows[0][1] is None
    assert output.model_dump(mode="json")["rows"] == [["298.90", None]]


@pytest.mark.parametrize("state,exception", [("57014", AnalyticalQueryTimeout), ("08006", AnalyticalQueryError)])
def test_error_mapping_and_rollback(state, exception):
    engine, connection, _ = fake_engine()
    original = Exception("postgresql://user:secret_password@host/db")
    original.sqlstate = state
    connection.exec_driver_sql.side_effect = [None, None, None, OperationalError("secret_sql", None, original)]
    with pytest.raises(exception) as error:
        execute_analytical_query(engine, validate_analytical_sql("SELECT 1"))
    assert "secret" not in str(error.value)
    assert "postgresql://" not in str(error.value)
    connection.begin.return_value.rollback.assert_called_once()


def test_connection_failure_safe():
    engine, _, _ = fake_engine()
    engine.connect.side_effect = OperationalError(None, None, Exception("secret_password"))
    with pytest.raises(AnalyticalQueryError, match="Analytical query failed"):
        execute_analytical_query(engine, validate_analytical_sql("SELECT 1"))


@pytest.mark.parametrize("query", ["SELECT 1", ValidatedQuery("DELETE FROM orders", ("orders",)), ValidatedQuery("SELECT 1", ("orders",))])
def test_raw_or_forged_contract_rejected_before_connection(query):
    engine, _, _ = fake_engine()
    with pytest.raises((ValueError, TypeError)):
        execute_analytical_query(engine, query)
    engine.connect.assert_not_called()


@pytest.mark.parametrize("options", [{"max_rows": 0}, {"max_rows": 201}, {"max_rows": True}, {"timeout_ms": 0}, {"timeout_ms": 3001}, {"timeout_ms": "3000"}])
def test_resource_limits_cannot_be_disabled(options):
    engine, _, _ = fake_engine()
    with pytest.raises(ValueError):
        execute_analytical_query(engine, validate_analytical_sql("SELECT 1"), **options)
    engine.connect.assert_not_called()
