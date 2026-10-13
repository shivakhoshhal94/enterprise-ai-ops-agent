import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Seller
from enterprise_ai_ops_agent.ingestion import sellers


def csv_text(rows: list[list[str]], header: tuple[str, ...] = sellers.SOURCE_COLUMNS) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def sample_rows() -> list[list[str]]:
    return [["seller1", "00123", " São Paulo ", "sp"], ["seller2", "00001", "Other City", "SP"]]


def test_parsing_preserves_source_strings() -> None:
    result = sellers.parse_sellers_csv(io.StringIO(csv_text(sample_rows())))
    assert result == [dict(zip(sellers.SOURCE_COLUMNS, row)) for row in sample_rows()]
    assert result[0]["seller_zip_code_prefix"] == "00123"
    assert isinstance(result[0]["seller_zip_code_prefix"], str)


@pytest.mark.parametrize("header", [sellers.SOURCE_COLUMNS[:-1], ("seller_id",)*4, sellers.SOURCE_COLUMNS + ("extra",)])
def test_wrong_headers(header: tuple[str, ...]) -> None:
    with pytest.raises(sellers.SellersIngestionError, match="header"):
        sellers.parse_sellers_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_fields(index: int, value: str) -> None:
    rows = sample_rows()
    rows[0][index] = value
    with pytest.raises(sellers.SellersIngestionError, match="Blank"):
        sellers.parse_sellers_csv(io.StringIO(csv_text(rows)))


def test_duplicate_seller_id() -> None:
    rows = sample_rows()
    rows[1][0] = rows[0][0]
    with pytest.raises(sellers.SellersIngestionError, match="Duplicate seller_id"):
        sellers.parse_sellers_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("postal", ["123", "123456", "abcde", " 0123", "１２３４５"])
def test_invalid_postal(postal: str) -> None:
    rows = sample_rows()
    rows[0][1] = postal
    with pytest.raises(sellers.SellersIngestionError, match="Postal prefix"):
        sellers.parse_sellers_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("state", ["S", "SPX", " SP "])
def test_invalid_state_length(state: str) -> None:
    rows = sample_rows()
    rows[0][3] = state
    with pytest.raises(sellers.SellersIngestionError, match="two characters"):
        sellers.parse_sellers_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("rows", [[], [["few"]], [sample_rows()[0] + ["extra"]]])
def test_empty_or_wrong_structure(rows: list[list[str]]) -> None:
    with pytest.raises(sellers.SellersIngestionError):
        sellers.parse_sellers_csv(io.StringIO(csv_text(rows)))


def test_invalid_quoting() -> None:
    with pytest.raises(sellers.SellersIngestionError, match="quoting"):
        sellers.parse_sellers_csv(io.StringIO(",".join(sellers.SOURCE_COLUMNS) + '\n"unterminated'))


@pytest.mark.parametrize("case", ["success", "missing", "nonempty", "late_failure", "mismatch"])
def test_transactional_batches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    path = tmp_path / "sellers.csv"
    path.write_text(csv_text(sample_rows()), encoding="utf-8", newline="")
    engine = MagicMock(spec=Engine)
    connection = engine.begin.return_value.__enter__.return_value
    inspector = MagicMock()
    inspector.has_table.return_value = case != "missing"
    monkeypatch.setattr(sellers, "inspect", lambda conn: inspector)
    connection.execute.return_value.scalar_one.side_effect = [1] if case == "nonempty" else [0, 1 if case == "mismatch" else 2]
    if case == "late_failure":
        count = MagicMock()
        count.scalar_one.return_value = 0
        connection.execute.side_effect = [count, MagicMock(), OperationalError(None, None, Exception("secret_password"))]
    if case == "success":
        assert sellers.ingest_sellers(engine, path, 1) == {"source_rows_read": 2, "rows_inserted": 2, "target_table": "sellers"}
        inserts = [c for c in connection.execute.call_args_list if isinstance(c.args[0], Insert)]
        assert len(inserts) == 2
        assert all(c.args[0].table is Seller.__table__ for c in inserts)
        assert all(len(c.args[1]) == 1 for c in inserts)
        engine.begin.return_value.__exit__.assert_called_once_with(None, None, None)
    else:
        with pytest.raises(sellers.SellersIngestionError) as caught:
            sellers.ingest_sellers(engine, path, 1)
        assert "secret_password" not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {"missing", "nonempty"}:
            assert not any(isinstance(c.args[0], Insert) for c in connection.execute.call_args_list)


def test_missing_source_never_connects(tmp_path: Path) -> None:
    engine = MagicMock(spec=Engine)
    with pytest.raises(sellers.SellersIngestionError, match="Cannot open"):
        sellers.ingest_sellers(engine, tmp_path / "missing.csv")
    engine.begin.assert_not_called()


def test_invalid_source_never_connects(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("bad\n", encoding="utf-8")
    engine = MagicMock(spec=Engine)
    with pytest.raises(sellers.SellersIngestionError):
        sellers.ingest_sellers(engine, path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "invalid", "database", "success"])
def test_cli_safety(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str) -> None:
    settings = Settings(_env_file=None, database_url=None if case == "missing" else "postgresql+psycopg://localhost/test")
    monkeypatch.setattr(sellers, "Settings", lambda: settings)
    factory = MagicMock(return_value=MagicMock(spec=Engine))
    if case == "invalid":
        factory.side_effect = ValueError("secret_password")
    monkeypatch.setattr(sellers, "create_db_engine", factory)
    operation = MagicMock(return_value={"source_rows_read": 2, "rows_inserted": 2, "target_table": "sellers"})
    if case == "database":
        operation.side_effect = sellers.SellersIngestionError("Sellers ingestion failed.")
    monkeypatch.setattr(sellers, "ingest_sellers", operation)
    assert sellers.main() == (0 if case == "success" else 1)
    output = capsys.readouterr()
    assert "secret_password" not in output.out + output.err
    if case in {"database", "success"}:
        factory.return_value.dispose.assert_called_once_with()
    else:
        operation.assert_not_called()
