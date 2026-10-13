"""Validate and load Olist products with explicit nullable-field conversions."""

import csv
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Product
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_products_dataset.csv")
SOURCE_TO_SCHEMA = {
    "product_id": "product_id",
    "product_category_name": "product_category_name",
    "product_name_lenght": "product_name_length",
    "product_description_lenght": "product_description_length",
    "product_photos_qty": "product_photos_qty",
    "product_weight_g": "product_weight_g",
    "product_length_cm": "product_length_cm",
    "product_height_cm": "product_height_cm",
    "product_width_cm": "product_width_cm",
}
SOURCE_COLUMNS = tuple(SOURCE_TO_SCHEMA)
ProductRow = dict[str, str | int | None]


class ProductsIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_optional_integer(value: str, column: str, line: int) -> int | None:
    if not value.strip():
        return None
    numeric_text = value.strip()
    if re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", numeric_text) is None:
        raise ProductsIngestionError(f"Invalid integer-compatible {column} at line {line}.")
    number = Decimal(numeric_text)
    if not number.is_finite() or number != number.to_integral_value() or not -2147483648 <= number <= 2147483647:
        raise ProductsIngestionError(f"Invalid integer-compatible {column} at line {line}.")
    return int(number)


def parse_products_csv(source: TextIO) -> list[ProductRow]:
    reader = csv.reader(source, strict=True)
    rows: list[ProductRow] = []
    seen_ids: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != len(SOURCE_COLUMNS) or set(header) != set(SOURCE_COLUMNS):
            raise ProductsIngestionError("CSV header must contain exactly the nine expected source product columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != len(SOURCE_COLUMNS):
                raise ProductsIngestionError(f"CSV structure error at line {line}: expected nine fields.")
            source_row = dict(zip(header, values))
            product_id = source_row["product_id"]
            if not product_id.strip():
                raise ProductsIngestionError(f"Blank product_id at line {line}.")
            if product_id in seen_ids:
                raise ProductsIngestionError(f"Duplicate product_id at line {line}.")
            seen_ids.add(product_id)
            row: ProductRow = {"product_id": product_id, "product_category_name": source_row["product_category_name"] or None}
            for name in SOURCE_COLUMNS[2:]:
                row[SOURCE_TO_SCHEMA[name]] = parse_optional_integer(source_row[name], name, line)
            rows.append(row)
    except (csv.Error, UnicodeError):
        raise ProductsIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise ProductsIngestionError("CSV contains no product data rows.")
    return rows


def read_products_source(path: Path) -> list[ProductRow]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_products_csv(source)
    except OSError:
        raise ProductsIngestionError("Cannot open products CSV; run from the repository root and check the source file.") from None


def ingest_products(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise ProductsIngestionError("Batch size must be positive.")
    rows = read_products_source(source_path)
    table = Product.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Products", ProductsIngestionError)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, ProductsIngestionError)
    except SQLAlchemyError:
        raise ProductsIngestionError("Products ingestion failed; transaction rolled back. Check database access and constraints.") from None
    return {"source_rows_read": len(rows), "rows_inserted": inserted, "target_table": table.name}


def main() -> int:
    try:
        settings = Settings()
        if settings.database_url is None or not settings.database_url.get_secret_value().strip():
            print("Database configuration error: set DATABASE_URL.", file=sys.stderr)
            return 1
        engine = create_db_engine(settings)
    except (ValueError, SQLAlchemyError):
        print("Database configuration error: check settings and DATABASE_URL.", file=sys.stderr)
        return 1
    try:
        summary = ingest_products(engine)
    except ProductsIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
