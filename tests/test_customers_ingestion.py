import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Customer
from enterprise_ai_ops_agent.ingestion import customers


def csv_text(rows: list[list[str]], header: tuple[str, ...] = customers.SOURCE_COLUMNS) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def sample_rows() -> list[list[str]]:
    return [["id1", "repeat_identity", "00123", " São Paulo ", "sp"],
            ["id2", "repeat_identity", "00001", "Other City", "SP"]]


def test_values_and_repeat_identity_preserved() -> None:
    result = customers.parse_customers_csv(io.StringIO(csv_text(sample_rows())))
    assert result[0] == dict(zip(customers.SOURCE_COLUMNS, sample_rows()[0]))
    assert result[1]["customer_unique_id"] == result[0]["customer_unique_id"]
    assert result[0]["customer_zip_code_prefix"] == "00123"
    assert isinstance(result[0]["customer_zip_code_prefix"], str)


@pytest.mark.parametrize("header", [customers.SOURCE_COLUMNS[:-1], ("customer_id",)*5, customers.SOURCE_COLUMNS + ("extra",)])
def test_bad_headers(header: tuple[str, ...]) -> None:
    with pytest.raises(customers.CustomersIngestionError, match="header"):
        customers.parse_customers_csv(io.StringIO(csv_text(sample_rows(), header)))


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_fields(index: int, value: str) -> None:
    rows = sample_rows()
    rows[0][index] = value
    with pytest.raises(customers.CustomersIngestionError, match="Blank"):
        customers.parse_customers_csv(io.StringIO(csv_text(rows)))


def test_duplicate_customer_id_rejected() -> None:
    rows = sample_rows()
    rows[1][0] = rows[0][0]
    with pytest.raises(customers.CustomersIngestionError, match="Duplicate customer_id"):
        customers.parse_customers_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("rows", [[], [["too", "few"]], [sample_rows()[0] + ["extra"]]])
def test_empty_or_bad_row_structure(rows: list[list[str]]) -> None:
    with pytest.raises(customers.CustomersIngestionError):
        customers.parse_customers_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("postal", ["123", "123456", "abcde"])
def test_invalid_postal_prefix(postal: str) -> None:
    rows = sample_rows()
    rows[0][2] = postal
    with pytest.raises(customers.CustomersIngestionError, match="Postal prefix"):
        customers.parse_customers_csv(io.StringIO(csv_text(rows)))


def test_missing_source(tmp_path: Path) -> None:
    with pytest.raises(customers.CustomersIngestionError, match="Cannot open"):
        customers.read_customers_source(tmp_path / "missing.csv")


def prepare_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, counts: list[int]) -> tuple[MagicMock, MagicMock, Path]:
    path = tmp_path / "customers.csv"
    path.write_text(csv_text(sample_rows()), encoding="utf-8", newline="")
    engine = MagicMock(spec=Engine)
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.side_effect = counts
    inspector = MagicMock()
    monkeypatch.setattr(customers, "inspect", lambda conn: inspector)
    return engine, inspector, path


def test_batch_insert_and_reconciliation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine, _, path = prepare_load(monkeypatch, tmp_path, [0, 2])
    assert customers.ingest_customers(engine, path, batch_size=1) == {
        "source_rows_read": 2, "rows_inserted": 2, "target_table": "customers",
    }
    connection = engine.begin.return_value.__enter__.return_value
    inserts = [call for call in connection.execute.call_args_list if isinstance(call.args[0], Insert)]
    assert len(inserts) == 2
    assert all(call.args[0].table is Customer.__table__ for call in inserts)
    assert all(len(call.args[1]) == 1 for call in inserts)
    engine.begin.assert_called_once()
    engine.begin.return_value.__exit__.assert_called_once_with(None, None, None)


def test_larger_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine, _, path = prepare_load(monkeypatch, tmp_path, [0, 2])
    customers.ingest_customers(engine, path, batch_size=1000)
    calls = engine.begin.return_value.__enter__.return_value.execute.call_args_list
    assert len([call for call in calls if isinstance(call.args[0], Insert)]) == 1


@pytest.mark.parametrize("reason", ["missing", "nonempty", "mismatch", "database"])
def test_failure_exits_transaction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str) -> None:
    engine, inspector, path = prepare_load(monkeypatch, tmp_path, [1] if reason == "nonempty" else [0, 1])
    connection = engine.begin.return_value.__enter__.return_value
    if reason == "missing":
        inspector.has_table.return_value = False
    elif reason == "database":
        connection.execute.side_effect = OperationalError(None, None, Exception("secret_password"))
    with pytest.raises(customers.CustomersIngestionError) as caught:
        customers.ingest_customers(engine, path)
    assert "secret_password" not in str(caught.value)
    assert engine.begin.return_value.__exit__.call_args.args[0] is not None
    if reason in {"missing", "nonempty", "database"}:
        assert not any(isinstance(call.args[0], Insert) for call in connection.execute.call_args_list)
    if reason == "missing":
        assert "create_schema" in str(caught.value)
    if reason == "nonempty":
        assert "not empty" in str(caught.value)


def test_invalid_source_never_opens_transaction(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("bad\n", encoding="utf-8")
    engine = MagicMock(spec=Engine)
    with pytest.raises(customers.CustomersIngestionError):
        customers.ingest_customers(engine, path)
    engine.begin.assert_not_called()


def test_later_batch_failure_rolls_back_entire_operation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine, _, path = prepare_load(monkeypatch, tmp_path, [0])
    connection = engine.begin.return_value.__enter__.return_value
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    connection.execute.side_effect = [count_result, MagicMock(), OperationalError(None, None, Exception("secret_password"))]
    with pytest.raises(customers.CustomersIngestionError, match="rolled back"):
        customers.ingest_customers(engine, path, batch_size=1)
    assert connection.execute.call_count == 3
    assert engine.begin.return_value.__exit__.call_args.args[0] is OperationalError


def test_invalid_quoting() -> None:
    content = ",".join(customers.SOURCE_COLUMNS) + '\n"unterminated'
    with pytest.raises(customers.CustomersIngestionError, match="quoting"):
        customers.parse_customers_csv(io.StringIO(content))


@pytest.mark.parametrize("case", ["missing", "invalid", "database", "success"])
def test_cli_results_and_credential_safety(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str) -> None:
    settings = Settings(_env_file=None, database_url=None if case == "missing" else "postgresql+psycopg://localhost/test")
    monkeypatch.setattr(customers, "Settings", lambda: settings)
    factory = MagicMock(return_value=MagicMock(spec=Engine))
    if case == "invalid":
        factory.side_effect = ValueError("secret_password")
    monkeypatch.setattr(customers, "create_db_engine", factory)
    operation = MagicMock(return_value={"source_rows_read": 2, "rows_inserted": 2, "target_table": "customers"})
    if case == "database":
        operation.side_effect = customers.CustomersIngestionError("Database ingestion failed.")
    monkeypatch.setattr(customers, "ingest_customers", operation)
    assert customers.main() == (0 if case == "success" else 1)
    output = capsys.readouterr()
    assert "secret_password" not in output.out + output.err
    if case in {"success", "database"}:
        factory.return_value.dispose.assert_called_once_with()
    else:
        operation.assert_not_called()
