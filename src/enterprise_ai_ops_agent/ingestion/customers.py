"""Validate and load only the Olist customers CSV into an empty table."""

import csv
import json
import re
import sys
from pathlib import Path
from typing import TextIO, TypedDict

from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Customer
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_customers_dataset.csv")
SOURCE_COLUMNS = (
    "customer_id", "customer_unique_id", "customer_zip_code_prefix",
    "customer_city", "customer_state",
)


class CustomersIngestionError(RuntimeError):
    """A source or database failure with a credential-safe message."""


class IngestionSummary(TypedDict):
    source_rows_read: int
    rows_inserted: int
    target_table: str


def parse_customers_csv(source: TextIO) -> list[dict[str, str]]:
    """Validate the entire CSV, preserving source values exactly."""
    reader = csv.reader(source, strict=True)
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != len(SOURCE_COLUMNS) or set(header) != set(SOURCE_COLUMNS):
            raise CustomersIngestionError("CSV header must contain exactly the five expected customer columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != len(header):
                raise CustomersIngestionError(f"CSV structure error at line {line}: expected five fields.")
            row = dict(zip(header, values))
            for name in SOURCE_COLUMNS:
                if not row[name].strip():
                    raise CustomersIngestionError(f"Blank {name} at line {line}.")
            if re.fullmatch(r"[0-9]{5}", row["customer_zip_code_prefix"]) is None:
                raise CustomersIngestionError(f"Postal prefix must be five digits at line {line}.")
            if len(row["customer_state"]) != 2:
                raise CustomersIngestionError(f"State must contain two characters at line {line}.")
            if row["customer_id"] in seen_ids:
                raise CustomersIngestionError(f"Duplicate customer_id at line {line}.")
            seen_ids.add(row["customer_id"])
            rows.append(row)
    except (csv.Error, UnicodeError):
        raise CustomersIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise CustomersIngestionError("CSV contains no customer data rows.")
    return rows


def read_customers_source(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_customers_csv(source)
    except OSError:
        raise CustomersIngestionError("Cannot open customers CSV; run from the repository root and check the source file.") from None


def ingest_customers(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> IngestionSummary:
    if batch_size < 1:
        raise CustomersIngestionError("Batch size must be positive.")
    rows = read_customers_source(source_path)
    table = Customer.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Customers", CustomersIngestionError)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, CustomersIngestionError)
    except SQLAlchemyError:
        raise CustomersIngestionError("Customers ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_customers(engine)
    except CustomersIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
