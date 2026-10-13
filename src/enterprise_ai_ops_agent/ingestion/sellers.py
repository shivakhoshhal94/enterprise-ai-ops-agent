"""Validate and load only Olist sellers into an existing empty table."""

import csv
import json
import re
import sys
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import Seller
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/olist_sellers_dataset.csv")
SOURCE_COLUMNS = ("seller_id", "seller_zip_code_prefix", "seller_city", "seller_state")


class SellersIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_sellers_csv(source: TextIO) -> list[dict[str, str]]:
    reader = csv.reader(source, strict=True)
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != 4 or set(header) != set(SOURCE_COLUMNS):
            raise SellersIngestionError("CSV header must contain exactly the four expected seller columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != 4:
                raise SellersIngestionError(f"CSV structure error at line {line}: expected four fields.")
            row = dict(zip(header, values))
            for name in SOURCE_COLUMNS:
                if not row[name].strip():
                    raise SellersIngestionError(f"Blank {name} at line {line}.")
            if re.fullmatch(r"[0-9]{5}", row["seller_zip_code_prefix"]) is None:
                raise SellersIngestionError(f"Postal prefix must be five digits at line {line}.")
            if len(row["seller_state"]) != 2:
                raise SellersIngestionError(f"State must contain two characters at line {line}.")
            if row["seller_id"] in seen_ids:
                raise SellersIngestionError(f"Duplicate seller_id at line {line}.")
            seen_ids.add(row["seller_id"])
            rows.append(row)
    except (csv.Error, UnicodeError):
        raise SellersIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise SellersIngestionError("CSV contains no seller data rows.")
    return rows


def read_sellers_source(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_sellers_csv(source)
    except OSError:
        raise SellersIngestionError("Cannot open sellers CSV; run from the repository root and check the source file.") from None


def ingest_sellers(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise SellersIngestionError("Batch size must be positive.")
    rows = read_sellers_source(source_path)
    table = Seller.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Sellers", SellersIngestionError)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, SellersIngestionError)
    except SQLAlchemyError:
        raise SellersIngestionError("Sellers ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_sellers(engine)
    except SellersIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
