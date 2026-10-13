import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.ingestion import category_translations as loader


def csv_text(rows: list[list[str]], header: tuple[str, ...] = loader.SOURCE_COLUMNS) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_original_values_preserved() -> None:
    rows = [[" CATEGÓRIA ", " English label "], ["other", "same translation"]]
    assert loader.parse_category_translations_csv(io.StringIO(csv_text(rows))) == [dict(zip(loader.SOURCE_COLUMNS, row)) for row in rows]


@pytest.mark.parametrize("header", [("wrong",), ("product_category_name",)*2, loader.SOURCE_COLUMNS + ("extra",)])
def test_invalid_headers(header: tuple[str, ...]) -> None:
    with pytest.raises(loader.CategoryTranslationsIngestionError, match="header"):
        loader.parse_category_translations_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_values(index: int, value: str) -> None:
    row = ["category", "translation"]
    row[index] = value
    with pytest.raises(loader.CategoryTranslationsIngestionError, match="Blank"):
        loader.parse_category_translations_csv(io.StringIO(csv_text([row])))


def test_duplicate_portuguese_key() -> None:
    with pytest.raises(loader.CategoryTranslationsIngestionError, match="Duplicate"):
        loader.parse_category_translations_csv(io.StringIO(csv_text([["same", "one"], ["same", "two"]])))


@pytest.mark.parametrize("rows", [[], [["one"]], [["one", "two", "three"]]])
def test_empty_or_wrong_field_count(rows: list[list[str]]) -> None:
    with pytest.raises(loader.CategoryTranslationsIngestionError):
        loader.parse_category_translations_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("case", ["success", "nonempty", "missing", "late_failure", "mismatch"])
def test_transactional_loading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    path = tmp_path / "translations.csv"
    path.write_text(csv_text([["one", "ONE"], ["two", "TWO"]]), encoding="utf-8", newline="")
    engine = MagicMock(spec=Engine)
    connection = engine.begin.return_value.__enter__.return_value
    inspector = MagicMock()
    inspector.has_table.return_value = case != "missing"
    monkeypatch.setattr(loader, "inspect", lambda conn: inspector)
    connection.execute.return_value.scalar_one.side_effect = [1] if case == "nonempty" else [0, 1 if case == "mismatch" else 2]
    if case == "late_failure":
        count = MagicMock()
        count.scalar_one.return_value = 0
        connection.execute.side_effect = [count, MagicMock(), OperationalError(None, None, Exception("secret_password"))]
    if case == "success":
        assert loader.ingest_category_translations(engine, path, 1) == {"source_rows_read": 2, "rows_inserted": 2, "target_table": "category_translations"}
        inserts = [c for c in connection.execute.call_args_list if isinstance(c.args[0], Insert)]
        assert len(inserts) == 2
        engine.begin.return_value.__exit__.assert_called_once_with(None, None, None)
    else:
        with pytest.raises(loader.CategoryTranslationsIngestionError) as caught:
            loader.ingest_category_translations(engine, path, 1)
        assert "secret_password" not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {"missing", "nonempty"}:
            assert not any(isinstance(c.args[0], Insert) for c in connection.execute.call_args_list)


def test_invalid_source_does_not_connect(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("bad\n", encoding="utf-8")
    engine = MagicMock(spec=Engine)
    with pytest.raises(loader.CategoryTranslationsIngestionError):
        loader.ingest_category_translations(engine, path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "invalid", "database", "success"])
def test_cli_safety(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str) -> None:
    settings = Settings(_env_file=None, database_url=None if case == "missing" else "postgresql+psycopg://localhost/test")
    monkeypatch.setattr(loader, "Settings", lambda: settings)
    factory = MagicMock(return_value=MagicMock(spec=Engine))
    if case == "invalid":
        factory.side_effect = ValueError("secret_password")
    monkeypatch.setattr(loader, "create_db_engine", factory)
    operation = MagicMock(return_value={"source_rows_read": 2, "rows_inserted": 2, "target_table": "category_translations"})
    if case == "database":
        operation.side_effect = loader.CategoryTranslationsIngestionError("Category translations ingestion failed.")
    monkeypatch.setattr(loader, "ingest_category_translations", operation)
    assert loader.main() == (0 if case == "success" else 1)
    output = capsys.readouterr()
    assert "secret_password" not in output.err + output.out
    if case in {"success", "database"}:
        factory.return_value.dispose.assert_called_once_with()
    else:
        operation.assert_not_called()
