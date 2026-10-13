"""Explicit item-grain ingestion with exact money and three-parent validation."""

import csv
import json
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from sqlalchemy import Connection, Engine, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Order, OrderItem, Product, Seller
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_order_items_dataset.csv")
SOURCE_COLUMNS = (
    "order_id", "order_item_id", "product_id", "seller_id",
    "shipping_limit_date", "price", "freight_value",
)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
NUMERIC_PATTERN = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
ItemRow = dict[str, str | int | datetime | Decimal]


class OrderItemsIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_number(value: str, column: str, line: int) -> Decimal:
    if re.fullmatch(NUMERIC_PATTERN, value.strip()) is None:
        raise OrderItemsIngestionError(f"Invalid numeric value in {column} at line {line}.")
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        raise OrderItemsIngestionError(f"Invalid numeric value in {column} at line {line}.") from None


def parse_item_id(value: str, line: int) -> int:
    number = parse_number(value, "order_item_id", line)
    if not 1 <= number <= 2147483647 or number != number.to_integral_value():
        raise OrderItemsIngestionError(f"Invalid positive integer-compatible order_item_id at line {line}.")
    return int(number)


def parse_money(value: str, column: str, line: int) -> Decimal:
    number = parse_number(value, column, line)
    # Reject values PostgreSQL NUMERIC(12,2) would round or cannot represent.
    if number.copy_abs() > Decimal("9999999999.99") or number != number.quantize(Decimal("0.01")):
        raise OrderItemsIngestionError(f"Invalid NUMERIC(12,2) value in {column} at line {line}.")
    return number


def parse_deadline(value: str, line: int) -> datetime:
    try:
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
        if parsed.strftime(TIMESTAMP_FORMAT) != value:
            raise ValueError
        return parsed
    except ValueError:
        raise OrderItemsIngestionError(f"Invalid shipping_limit_date at line {line}; expected YYYY-MM-DD HH:MM:SS.") from None


def parse_order_items_csv(source: TextIO) -> list[ItemRow]:
    reader = csv.reader(source, strict=True)
    rows: list[ItemRow] = []
    seen_keys: set[tuple[str, int]] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != len(SOURCE_COLUMNS) or set(header) != set(SOURCE_COLUMNS):
            raise OrderItemsIngestionError("CSV header must contain exactly the seven expected order item columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != len(SOURCE_COLUMNS):
                raise OrderItemsIngestionError(f"CSV structure error at line {line}: expected seven fields.")
            source_row = dict(zip(header, values))
            for name in SOURCE_COLUMNS:
                if not source_row[name].strip():
                    raise OrderItemsIngestionError(f"Blank {name} at line {line}.")
            item_id = parse_item_id(source_row["order_item_id"], line)
            key = (source_row["order_id"], item_id)
            if key in seen_keys:
                raise OrderItemsIngestionError(f"Duplicate (order_id, order_item_id) at line {line}.")
            seen_keys.add(key)
            rows.append({
                "order_id": source_row["order_id"], "order_item_id": item_id,
                "product_id": source_row["product_id"], "seller_id": source_row["seller_id"],
                "shipping_limit_date": parse_deadline(source_row["shipping_limit_date"], line),
                "price": parse_money(source_row["price"], "price", line),
                "freight_value": parse_money(source_row["freight_value"], "freight_value", line),
            })
    except (csv.Error, UnicodeError):
        raise OrderItemsIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise OrderItemsIngestionError("CSV contains no order item data rows.")
    return rows


def validate_parent_ids(connection: Connection, rows: list[ItemRow]) -> None:
    """Three queries regardless of item count; database foreign keys remain enforced."""
    failures = []
    for name, column in (("order_id", Order.order_id), ("product_id", Product.product_id), ("seller_id", Seller.seller_id)):
        existing = set(connection.execute(select(column)).scalars())
        missing = sorted({row[name] for row in rows} - existing)
        if missing:
            sample = json.dumps([value[:64] for value in missing[:3]])
            failures.append(f"{name}: {len(missing)} distinct unmatched IDs; sample {sample}")
    if failures:
        raise OrderItemsIngestionError("; ".join(failures) + ". Order items were not loaded.")


def read_order_items_source(path: Path) -> list[ItemRow]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_order_items_csv(source)
    except OSError:
        raise OrderItemsIngestionError("Cannot open order_items CSV; run from the repository root and check the source file.") from None


def ingest_order_items(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise OrderItemsIngestionError("Batch size must be positive.")
    rows = read_order_items_source(source_path)
    table = OrderItem.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Order items", OrderItemsIngestionError)
            validate_parent_ids(connection, rows)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, OrderItemsIngestionError)
    except SQLAlchemyError:
        raise OrderItemsIngestionError("Order items ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_order_items(engine)
    except OrderItemsIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
