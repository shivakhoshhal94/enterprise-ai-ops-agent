"""Explicit orders ingestion with naive timestamps and customer FK validation."""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from sqlalchemy import Connection, Engine, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Customer, Order
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_orders_dataset.csv")
SOURCE_COLUMNS = (
    "order_id", "customer_id", "order_status", "order_purchase_timestamp",
    "order_approved_at", "order_delivered_carrier_date",
    "order_delivered_customer_date", "order_estimated_delivery_date",
)
OPTIONAL_TIMESTAMPS = frozenset(SOURCE_COLUMNS[4:7])
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
OrderRow = dict[str, str | datetime | None]


class OrdersIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_timestamp(value: str, column: str, line: int) -> datetime | None:
    if not value.strip():
        if column in OPTIONAL_TIMESTAMPS:
            return None
        raise OrdersIngestionError(f"Blank {column} at line {line}.")
    try:
        parsed = datetime.strptime(value, TIMESTAMP_FORMAT)
        if parsed.strftime(TIMESTAMP_FORMAT) != value:
            raise ValueError
        return parsed
    except ValueError:
        raise OrdersIngestionError(f"Invalid timestamp in {column} at line {line}; expected YYYY-MM-DD HH:MM:SS.") from None


def parse_orders_csv(source: TextIO) -> list[OrderRow]:
    reader = csv.reader(source, strict=True)
    rows: list[OrderRow] = []
    seen_ids: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != len(SOURCE_COLUMNS) or set(header) != set(SOURCE_COLUMNS):
            raise OrdersIngestionError("CSV header must contain exactly the eight expected order columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != len(SOURCE_COLUMNS):
                raise OrdersIngestionError(f"CSV structure error at line {line}: expected eight fields.")
            source_row = dict(zip(header, values))
            for name in SOURCE_COLUMNS[:3]:
                if not source_row[name].strip():
                    raise OrdersIngestionError(f"Blank {name} at line {line}.")
            order_id = source_row["order_id"]
            if order_id in seen_ids:
                raise OrdersIngestionError(f"Duplicate order_id at line {line}.")
            seen_ids.add(order_id)
            row: OrderRow = {name: source_row[name] for name in SOURCE_COLUMNS[:3]}
            for name in SOURCE_COLUMNS[3:]:
                row[name] = parse_timestamp(source_row[name], name, line)
            rows.append(row)
    except (csv.Error, UnicodeError):
        raise OrdersIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise OrdersIngestionError("CSV contains no order data rows.")
    return rows


def validate_customer_ids(connection: Connection, rows: list[OrderRow]) -> None:
    """One parent-ID query; the database FK still enforces integrity at insertion."""
    existing = set(connection.execute(select(Customer.customer_id)).scalars())
    missing = sorted({row["customer_id"] for row in rows} - existing)
    if missing:
        sample = json.dumps([value[:64] for value in missing[:3]])
        raise OrdersIngestionError(f"Unmatched customer IDs: {len(missing)} distinct; sample {sample}. Orders were not loaded.")


def read_orders_source(path: Path) -> list[OrderRow]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_orders_csv(source)
    except OSError:
        raise OrdersIngestionError("Cannot open orders CSV; run from the repository root and check the source file.") from None


def ingest_orders(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise OrdersIngestionError("Batch size must be positive.")
    rows = read_orders_source(source_path)
    table = Order.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Orders", OrdersIngestionError)
            validate_customer_ids(connection, rows)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, OrdersIngestionError)
    except SQLAlchemyError:
        raise OrdersIngestionError("Orders ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_orders(engine)
    except OrdersIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
