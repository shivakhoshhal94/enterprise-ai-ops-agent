"""Explicit read-only connectivity check and command-line entry point."""

import sys

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine


class DatabaseConnectionError(RuntimeError):
    """Connectivity failed; the message contains no connection credentials."""


def check_database_connection(engine: Engine) -> bool:
    """Return True when SELECT 1 succeeds; otherwise raise a safe error."""
    try:
        with engine.connect() as connection:
            value = connection.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError:
        raise DatabaseConnectionError("Database connection check failed.") from None
    if value != 1:
        raise DatabaseConnectionError("Database connection check returned an unexpected result.")
    return True


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
        check_database_connection(engine)
    except DatabaseConnectionError:
        print("Database connection failed: check server availability and credentials.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print("Database connection successful (SELECT 1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
