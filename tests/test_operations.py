"""Offline action tests with real transactions; PostgreSQL is checked separately."""

from datetime import timezone
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, create_engine, event, func, insert, select
from sqlalchemy.dialects.sqlite.base import DATETIME
from sqlalchemy.exc import IntegrityError, OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db.models import Customer, Order
from enterprise_ai_ops_agent.operations import cli, service
from enterprise_ai_ops_agent.operations.contracts import CreateOrderReviewCaseRequest
from enterprise_ai_ops_agent.operations.models import OperationsBase, OrderReviewCase, CaseEvent

ORDER = "03ecec245220b63fd7f68c1737ba99ba"


def request(**changes):
    return CreateOrderReviewCaseRequest(**dict(
        {"order_id": ORDER, "reason": "Synthetic internal demonstration",
         "priority": "medium", "idempotency_key": "unit-demo"}, **changes,
    ))


class UTCDateTime(DATETIME):
    """SQLite loses tzinfo; restore UTC only in this test dialect."""
    def result_processor(self, dialect, coltype):
        base = super().result_processor(dialect, coltype)
        def process(value):
            result = base(value)
            return result.replace(tzinfo=timezone.utc) if result is not None else None
        return process


@pytest.fixture
def database():
    engine = create_engine("sqlite://", execution_options={"schema_translate_map": {"operations": None}})
    engine.dialect.colspecs = {**engine.dialect.colspecs, DateTime: UTCDateTime}
    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _):
        # SQLite's legacy transaction mode otherwise releases a first savepoint
        # as a commit. Use explicit BEGIN to model PostgreSQL transaction behavior.
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")
    Base.metadata.create_all(engine)
    OperationsBase.metadata.create_all(engine)
    from datetime import datetime
    with engine.begin() as connection:
        connection.execute(insert(Customer), dict(customer_id="customer", customer_unique_id="unique",
            customer_zip_code_prefix="01234", customer_city="city", customer_state="SP"))
        connection.execute(insert(Order), dict(order_id=ORDER, customer_id="customer", order_status="delivered",
            order_purchase_timestamp=datetime(2018, 1, 1), order_estimated_delivery_date=datetime(2018, 2, 1)))
    yield engine
    engine.dispose()


def counts(engine):
    with engine.connect() as connection:
        return tuple(connection.scalar(select(func.count()).select_from(table))
                     for table in (OrderReviewCase, CaseEvent))


def test_valid_contract_and_extra_metadata_rejected():
    assert request().priority == "medium"
    with pytest.raises(ValidationError):
        request(created_by="fake-human")


@pytest.mark.parametrize("changes", [
    {"reason": ""}, {"reason": "  "}, {"reason": "x" * 1001},
    {"priority": "urgent"}, {"order_id": ""}, {"idempotency_key": " "},
    {"idempotency_key": "x" * 201},
])
def test_invalid_contract(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_missing_order_id():
    with pytest.raises(ValidationError):
        CreateOrderReviewCaseRequest(reason="review", priority="low", idempotency_key="key")


def test_creation_and_idempotency(database):
    result = service.create_order_review_case(database, request(), actor="developer_cli")
    assert isinstance(result.case_id, UUID)
    assert result.status == "open"
    assert result.created_at.utcoffset().total_seconds() == 0
    assert result.created_by == "developer_cli"
    with database.connect() as connection:
        row = connection.execute(select(CaseEvent.__table__)).mappings().one()
        assert row["case_id"] == result.case_id
        assert row["event_type"] == "case_created"
        assert row["actor"] == result.created_by
        assert row["created_at"] == result.created_at
    retry = service.create_order_review_case(database, request(), actor="other_application_source")
    assert retry == result
    assert counts(database) == (1, 1)


@pytest.mark.parametrize("changes", [
    {"order_id": "different"}, {"reason": "different"}, {"priority": "high"},
])
def test_conflicts(database, changes):
    service.create_order_review_case(database, request(), actor="developer_cli")
    with pytest.raises(service.IdempotencyConflict, match="different request"):
        service.create_order_review_case(database, request(**changes), actor="developer_cli")
    assert counts(database) == (1, 1)


def test_unknown_order(database):
    with pytest.raises(service.UnknownOrderError, match="does not exist"):
        service.create_order_review_case(database, request(order_id="unknown"), actor="developer_cli")
    assert counts(database) == (0, 0)


def test_audit_failure_rolls_back_and_no_historical_writes(database):
    statements = []
    @event.listens_for(database, "before_cursor_execute")
    def fail_audit(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if "INSERT INTO main.case_events" in statement:
            raise OperationalError(statement, parameters, Exception("private_password"))
    with pytest.raises(service.ActionError) as error:
        service.create_order_review_case(database, request(), actor="developer_cli")
    assert "private_password" not in str(error.value)
    assert error.value.__suppress_context__
    assert counts(database) == (0, 0)
    writes = [sql for sql in statements if sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(writes) == 2
    assert all("order_review_cases" in sql or "case_events" in sql for sql in writes)


def test_success_never_writes_historical_tables(database):
    statements = []
    event.listen(database, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
    service.create_order_review_case(database, request(), actor="developer_cli")
    writes = [sql for sql in statements if sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(writes) == 2
    assert all("order_review_cases" in sql or "case_events" in sql for sql in writes)


def test_unchecked_request_and_invalid_actor(database):
    invalid = request().model_copy(update={"reason": ""})
    with pytest.raises(service.ActionError, match="Invalid"):
        service.create_order_review_case(database, invalid, actor="developer_cli")
    with pytest.raises(service.ActionError, match="actor"):
        service.create_order_review_case(database, request(), actor=" ")


def test_competing_unique_key_insert_returns_winner(database, monkeypatch):
    winner = service.create_order_review_case(database, request(), actor="developer_cli")
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=ORDER)),
        IntegrityError(None, None, Exception("private_password")),
    ]
    lookup = MagicMock(side_effect=[None, winner])
    monkeypatch.setattr(service, "_existing_case", lookup)
    assert service.create_order_review_case(engine, request(), actor="developer_cli") == winner
    assert lookup.call_count == 2
    assert connection.execute.call_count == 2  # No audit insert by the loser.
    connection.begin_nested.assert_called_once()


def test_database_connection_failure_is_safe():
    engine = MagicMock()
    engine.begin.side_effect = OperationalError(None, None, Exception("private_password"))
    with pytest.raises(service.ActionError) as caught:
        service.create_order_review_case(engine, request(), actor="developer_cli")
    assert "private_password" not in str(caught.value)


def arguments(**changes):
    payload = request(**changes).model_dump()
    return ["create-order-review-case", *sum(
        (["--" + key.replace("_", "-"), value] for key, value in payload.items()), []
    )]


def test_cli_success(database, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "create_db_engine", lambda _: database)
    assert cli.main(arguments()) == 0
    assert '"status":"open"' in capsys.readouterr().out


def test_cli_validation_failure(monkeypatch, capsys):
    factory = MagicMock()
    monkeypatch.setattr(cli, "create_db_engine", factory)
    args = arguments()
    args[args.index("--priority") + 1] = "private_password"
    assert cli.main(args) == 1
    assert "private_password" not in str(capsys.readouterr())
    factory.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError("private_password"), OperationalError(None, None, Exception("private_password"))])
def test_cli_configuration_failure(monkeypatch, capsys, failure):
    monkeypatch.setattr(cli, "create_db_engine", MagicMock(side_effect=failure))
    assert cli.main(arguments()) == 1
    output = capsys.readouterr()
    assert "configuration error" in output.err
    assert "private_password" not in output.err + output.out


def test_cli_action_failure_disposes_engine(monkeypatch, capsys):
    engine = MagicMock()
    monkeypatch.setattr(cli, "create_db_engine", lambda _: engine)
    monkeypatch.setattr(cli, "create_order_review_case", MagicMock(
        side_effect=service.UnknownOrderError("Referenced historical order does not exist.")))
    assert cli.main(arguments()) == 1
    assert "does not exist" in capsys.readouterr().err
    engine.dispose.assert_called_once()


def test_metadata_and_migration_structure():
    assert len(Base.metadata.tables) == 7
    assert set(OperationsBase.metadata.tables) == {"operations.order_review_cases", "operations.case_events"}
    path = Path("migrations/versions/0001_order_review_cases.py")
    spec = importlib.util.spec_from_file_location("review_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operation = MagicMock()
    migration.op = operation
    migration.upgrade()
    assert [call.args[0] for call in operation.create_table.call_args_list] == ["order_review_cases", "case_events"]
    assert all(call.kwargs["schema"] == "operations" for call in operation.create_table.call_args_list)
    migration.downgrade()
    assert [call.args[0] for call in operation.drop_table.call_args_list] == ["case_events", "order_review_cases"]
    assert operation.execute.call_args.args == ("DROP SCHEMA operations",)
    assert "sqlalchemy.url" not in Path("alembic.ini").read_text()


def test_assistant_has_no_write_tool():
    from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
    assert {tool["name"] for tool in TOOL_SCHEMAS} == {
        "get_order_status_summary", "get_customer_order_history", "get_order_financial_summary", "run_analytical_query",
        "propose_order_review_case", "search_company_knowledge", "get_support_context",
    }


def test_offline_migration_ddl_requires_no_connection(monkeypatch):
    from io import StringIO
    from alembic import command
    from alembic.config import Config
    import enterprise_ai_ops_agent.db.session as sessions
    factory = MagicMock(side_effect=AssertionError("Offline migration connected"))
    monkeypatch.setattr(sessions, "create_db_engine", factory)
    output = StringIO()
    command.upgrade(Config("alembic.ini", output_buffer=output), "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE operations.order_review_cases" in sql
    assert "CREATE TABLE operations.case_events" in sql
    assert "REFERENCES public.orders" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "CREATE TABLE public.orders" not in sql
    factory.assert_not_called()
