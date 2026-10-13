import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Product
from enterprise_ai_ops_agent.ingestion import products


def sample_row() -> list[str]:
    return ["product1", " pc_gamer ", "10.0", "20", "1", "0", "30", "40", "50"]


def csv_text(rows: list[list[str]], header: tuple[str, ...] = products.SOURCE_COLUMNS) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def test_valid_mapping_and_original_strings() -> None:
    parsed = products.parse_products_csv(io.StringIO(csv_text([sample_row()])))[0]
    assert parsed == {
        "product_id": "product1", "product_category_name": " pc_gamer ",
        "product_name_length": 10, "product_description_length": 20,
        "product_photos_qty": 1, "product_weight_g": 0,
        "product_length_cm": 30, "product_height_cm": 40, "product_width_cm": 50,
    }
    assert "product_name_lenght" not in parsed
    assert "product_description_lenght" not in parsed


@pytest.mark.parametrize("header", [products.SOURCE_COLUMNS[:-1], ("product_id",)*9, products.SOURCE_COLUMNS + ("extra",), tuple(products.SOURCE_TO_SCHEMA.values())])
def test_source_headers(header: tuple[str, ...]) -> None:
    with pytest.raises(products.ProductsIngestionError, match="header"):
        products.parse_products_csv(io.StringIO(csv_text([], header)))


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_product_id(value: str) -> None:
    row = sample_row()
    row[0] = value
    with pytest.raises(products.ProductsIngestionError, match="Blank product_id"):
        products.parse_products_csv(io.StringIO(csv_text([row])))


def test_duplicate_product_id() -> None:
    with pytest.raises(products.ProductsIngestionError, match="Duplicate"):
        products.parse_products_csv(io.StringIO(csv_text([sample_row(), sample_row()])))


@pytest.mark.parametrize("index", range(2, 9))
def test_nullable_numeric_values(index: int) -> None:
    row = sample_row()
    row[index] = ""
    parsed = products.parse_products_csv(io.StringIO(csv_text([row])))[0]
    assert parsed[products.SOURCE_TO_SCHEMA[products.SOURCE_COLUMNS[index]]] is None


@pytest.mark.parametrize("value", ["bad", "1.5", "NaN", "Infinity", "1_0", "2147483648", "1e999", "-2147483649"])
def test_invalid_numeric(value: str) -> None:
    with pytest.raises(products.ProductsIngestionError, match="integer-compatible"):
        products.parse_optional_integer(value, "product_weight_g", 2)


@pytest.mark.parametrize("value,expected", [("0", 0), ("0.0", 0), ("12.0", 12), ("1e2", 100), ("", None), ("   ", None), ("-1", -1)])
def test_exact_integer_conversion(value: str, expected: int | None) -> None:
    assert products.parse_optional_integer(value, "field", 2) == expected


@pytest.mark.parametrize("category", ["", "pc_gamer", "portateis_cozinha_e_preparadores_de_alimentos", " Category "])
def test_nullable_and_untranslated_categories(category: str) -> None:
    row = sample_row()
    row[1] = category
    parsed = products.parse_products_csv(io.StringIO(csv_text([row])))[0]
    assert parsed["product_category_name"] == (category or None)


@pytest.mark.parametrize("rows", [[], [["few"]], [sample_row() + ["extra"]]])
def test_empty_or_wrong_structure(rows: list[list[str]]) -> None:
    with pytest.raises(products.ProductsIngestionError):
        products.parse_products_csv(io.StringIO(csv_text(rows)))


@pytest.mark.parametrize("case", ["success", "missing", "nonempty", "late_failure", "mismatch"])
def test_transactional_loading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    second = sample_row()
    second[0] = "product2"
    second[5] = ""
    path = tmp_path / "products.csv"
    path.write_text(csv_text([sample_row(), second]), encoding="utf-8", newline="")
    engine = MagicMock(spec=Engine)
    connection = engine.begin.return_value.__enter__.return_value
    inspector = MagicMock()
    inspector.has_table.return_value = case != "missing"
    monkeypatch.setattr(products, "inspect", lambda conn: inspector)
    connection.execute.return_value.scalar_one.side_effect = [1] if case == "nonempty" else [0, 1 if case == "mismatch" else 2]
    if case == "late_failure":
        count = MagicMock()
        count.scalar_one.return_value = 0
        connection.execute.side_effect = [count, MagicMock(), OperationalError(None, None, Exception("secret_password"))]
    if case == "success":
        assert products.ingest_products(engine, path, 1) == {"source_rows_read": 2, "rows_inserted": 2, "target_table": "products"}
        inserts = [c for c in connection.execute.call_args_list if isinstance(c.args[0], Insert)]
        assert len(inserts) == 2
        assert all(c.args[0].table is Product.__table__ for c in inserts)
        assert inserts[0].args[1][0]["product_weight_g"] == 0
        assert inserts[1].args[1][0]["product_weight_g"] is None
        engine.begin.return_value.__exit__.assert_called_once_with(None, None, None)
    else:
        with pytest.raises(products.ProductsIngestionError) as caught:
            products.ingest_products(engine, path, 1)
        assert "secret_password" not in str(caught.value)
        assert engine.begin.return_value.__exit__.call_args.args[0] is not None
        if case in {"missing", "nonempty"}:
            assert not any(isinstance(c.args[0], Insert) for c in connection.execute.call_args_list)


def test_invalid_source_never_connects(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("bad\n", encoding="utf-8")
    engine = MagicMock(spec=Engine)
    with pytest.raises(products.ProductsIngestionError):
        products.ingest_products(engine, path)
    engine.begin.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "invalid", "database", "success"])
def test_cli_safety(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str) -> None:
    settings = Settings(_env_file=None, database_url=None if case == "missing" else "postgresql+psycopg://localhost/test")
    monkeypatch.setattr(products, "Settings", lambda: settings)
    factory = MagicMock(return_value=MagicMock(spec=Engine))
    if case == "invalid":
        factory.side_effect = ValueError("secret_password")
    monkeypatch.setattr(products, "create_db_engine", factory)
    operation = MagicMock(return_value={"source_rows_read": 2, "rows_inserted": 2, "target_table": "products"})
    if case == "database":
        operation.side_effect = products.ProductsIngestionError("Products ingestion failed.")
    monkeypatch.setattr(products, "ingest_products", operation)
    assert products.main() == (0 if case == "success" else 1)
    output = capsys.readouterr()
    assert "secret_password" not in output.out + output.err
    if case in {"database", "success"}:
        factory.return_value.dispose.assert_called_once_with()
    else:
        operation.assert_not_called()
