"""Explicit initial schema creation; importing this module performs no DDL."""

import sys

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import models  # Register the existing model tables.
from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db.session import create_db_engine


class SchemaCreationError(RuntimeError):
    """Schema creation failed without exposing driver details or credentials."""


def create_database_schema(engine: Engine) -> None:
    """Create missing model tables atomically; never migrate or drop tables."""
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(bind=connection, checkfirst=True)
    except SQLAlchemyError:
        raise SchemaCreationError("Database schema creation failed.") from None


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
        create_database_schema(engine)
    except SchemaCreationError:
        print("Database schema creation failed: check connectivity and permissions.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print("Database schema ready (seven application tables; no data loaded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
