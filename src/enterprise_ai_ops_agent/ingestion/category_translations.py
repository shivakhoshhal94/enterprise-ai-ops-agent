"""Load only the original Olist category translations into an empty table."""

import csv
import json
import sys
from pathlib import Path
from typing import TextIO

from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.models import CategoryTranslation
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


SOURCE_PATH = Path("data/raw/product_category_name_translation.csv")
SOURCE_COLUMNS = ("product_category_name", "product_category_name_english")


class CategoryTranslationsIngestionError(RuntimeError):
    """Source/database failure with no driver details or credentials."""


def parse_category_translations_csv(source: TextIO) -> list[dict[str, str]]:
    reader = csv.reader(source, strict=True)
    rows: list[dict[str, str]] = []
    seen_categories: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or len(header) != 2 or set(header) != set(SOURCE_COLUMNS):
            raise CategoryTranslationsIngestionError("CSV header must contain exactly the two expected translation columns.")
        for values in reader:
            line = reader.line_num
            if len(values) != 2:
                raise CategoryTranslationsIngestionError(f"CSV structure error at line {line}: expected two fields.")
            row = dict(zip(header, values))
            for name in SOURCE_COLUMNS:
                if not row[name].strip():
                    raise CategoryTranslationsIngestionError(f"Blank {name} at line {line}.")
            category = row["product_category_name"]
            if category in seen_categories:
                raise CategoryTranslationsIngestionError(f"Duplicate product_category_name at line {line}.")
            seen_categories.add(category)
            rows.append(row)
    except (csv.Error, UnicodeError):
        raise CategoryTranslationsIngestionError("CSV encoding or quoting is invalid.") from None
    if not rows:
        raise CategoryTranslationsIngestionError("CSV contains no category translation data rows.")
    return rows


def read_category_translations_source(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return parse_category_translations_csv(source)
    except OSError:
        raise CategoryTranslationsIngestionError("Cannot open category translations CSV; run from the repository root and check the source file.") from None


def ingest_category_translations(engine: Engine, source_path: Path = SOURCE_PATH, batch_size: int = 1000) -> dict[str, int | str]:
    if batch_size < 1:
        raise CategoryTranslationsIngestionError("Batch size must be positive.")
    rows = read_category_translations_source(source_path)
    table = CategoryTranslation.__table__
    try:
        with engine.begin() as connection:
            require_empty_table(connection, table, inspect(connection), "Category translations", CategoryTranslationsIngestionError)
            inserted = insert_reconciled_rows(connection, table, rows, batch_size, CategoryTranslationsIngestionError)
    except SQLAlchemyError:
        raise CategoryTranslationsIngestionError("Category translations ingestion failed; transaction rolled back. Check database access and constraints.") from None
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
        summary = ingest_category_translations(engine)
    except CategoryTranslationsIngestionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
