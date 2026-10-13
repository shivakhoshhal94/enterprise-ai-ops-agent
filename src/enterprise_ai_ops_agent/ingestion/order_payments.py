"""Explicit payment-entry ingestion with exact money and order-reference validation."""

import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from sqlalchemy import Connection, Engine, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Order, OrderPayment
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_order_payments_dataset.csv")
SOURCE_COLUMNS = (
    "order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value",
)
NUMERIC_PATTERN = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
PaymentRow = dict[str, str | int | Decimal]


class OrderPaymentsIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_number(value: str, column: str, line: int) -> Decimal:
    if re.fullmatch(NUMERIC_PATTERN, value.strip()) is None:
        raise OrderPaymentsIngestionError(f"Invalid numeric value in {column} at line {line}.")
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        raise OrderPaymentsIngestionError(f"Invalid numeric value in {column} at line {line}.") from None


def parse_integer(value: str, column: str, line: int) -> int:
    number = parse_number(value, column, line)
    if not -2147483648 <= number <= 2147483647 or number != number.to_integral_value():
        raise OrderPaymentsIngestionError(f"Invalid integer-compatible {column} at line {line}.")
    return int(number)


def parse_money(value: str, column: str, line: int) -> Decimal:
    number = parse_number(value, column, line)
    # Reject values PostgreSQL NUMERIC(12,2) would round or cannot represent.
    if number.copy_abs() > Decimal("9999999999.99") or number != number.quantize(Decimal("0.01")):
        raise OrderPaymentsIngestionError(f"Invalid NUMERIC(12,2) value in {column} at line {line}.")
    return number


def parse_order_payments_csv(source: TextIO) -> list[PaymentRow]:
    reader = csv.reader(source, strict=True)
    rows: list[PaymentRow] = []
    seen_keys: set[tuple[str, int]] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != len(SOURCE_COLUMNS) or set(header) != set(SOURCE_COLUMNS):
            raise OrderPaymentsIngestionError("CSV header must contain exactly the five expected order payment columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != len(SOURCE_COLUMNS):
                raise OrderPaymentsIngestionError(f"CSV structure error at line {line}: expected five fields.")
            source_row = dict(zip(header, values))
            for name in SOURCE_COLUMNS:
                if not source_row[name].strip():
                    raise OrderPaymentsIngestionError(f"Blank {name} at line {line}.")
            sequence = parse_integer(source_row["payment_sequential"], "payment_sequential", line)
            key = (source_row["order_id"], sequence)
            if key in seen_keys:
                raise OrderPaymentsIngestionError(f"Duplicate (order_id, payment_sequential) at line {line}.")
            seen_keys.add(key)
            rows.append({
                "order_id": source_row["order_id"], "payment_sequential": sequence,
                "payment_type": source_row["payment_type"],
                "payment_installments": parse_integer(source_row["payment_installments"], "payment_installments", line),
                "payment_value": parse_money(source_row["payment_value"], "payment_value", line),
            })
    except (csv.Error, UnicodeError):
        raise OrderPaymentsIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise OrderPaymentsIngestionError("CSV contains no order payment data rows.")
    return rows


def validate_order_ids(connection: Connection, rows: list[PaymentRow]) -> None:
    """One parent-ID query; PostgreSQL's foreign key remains the final boundary."""
    existing = set(connection.execute(select(Order.order_id)).scalars())
    missing = sorted({row["order_id"] for row in rows} - existing)
    if missing:
        sample = json.dumps([value[:64] for value in missing[:3]])
        raise OrderPaymentsIngestionError(
            f"Unmatched order IDs: {len(missing)} distinct; sample {sample}. Payments were not loaded."
        )


def read_order_payments_source(path: Path) -> list[PaymentRow]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_order_payments_csv(source)
    except OSError:
        raise OrderPaymentsIngestionError("Cannot open order_payments CSV; run from the repository root and check the source file.") from None


def ingest_order_payments(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise OrderPaymentsIngestionError("Batch size must be positive.")
    rows = read_order_payments_source(source_path)
    table = OrderPayment.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Order payments", OrderPaymentsIngestionError)
            validate_order_ids(connection, rows)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, OrderPaymentsIngestionError)
    except SQLAlchemyError:
        raise OrderPaymentsIngestionError("Order payments ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_order_payments(engine)
    except OrderPaymentsIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
