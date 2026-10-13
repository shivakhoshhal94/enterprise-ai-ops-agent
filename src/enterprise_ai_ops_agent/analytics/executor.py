"""Explicit PostgreSQL read-only execution, bounded time and bounded rows."""

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.analytics.models import (
    AnalyticalQueryError, AnalyticalQueryTimeout, AnalyticalResult, ValidatedQuery,
)
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_analytics_engine

DEFAULT_MAX_ROWS = 100
MAX_RETURNED_ROWS = 200
STATEMENT_TIMEOUT_MS = 3000


def execute_configured_analytical_query(
    settings: Settings, query: ValidatedQuery, *, max_rows: int = DEFAULT_MAX_ROWS,
    timeout_ms: int = STATEMENT_TIMEOUT_MS,
) -> AnalyticalResult:
    """Production-style entry point: dedicated configuration, no administrative fallback."""
    try:
        engine = create_analytics_engine(settings)
    except (ValueError, SQLAlchemyError):
        raise AnalyticalQueryError("Analytics configuration error: check ANALYTICS_DATABASE_URL.") from None
    try:
        return execute_analytical_query(engine, query, max_rows=max_rows, timeout_ms=timeout_ms)
    finally:
        engine.dispose()


def execute_analytical_query(
    engine: Engine, query: ValidatedQuery, *, max_rows: int = DEFAULT_MAX_ROWS,
    timeout_ms: int = STATEMENT_TIMEOUT_MS,
) -> AnalyticalResult:
    if not isinstance(query, ValidatedQuery):
        raise TypeError("Executor requires a ValidatedQuery.")
    # A Python type is not a security boundary: reject forged or stale contracts.
    if validate_analytical_sql(query.normalized_sql) != query:
        raise ValueError("Validated query contract does not match the SQL policy.")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_RETURNED_ROWS:
        raise ValueError("Row cap must be between 1 and 200.")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= STATEMENT_TIMEOUT_MS:
        raise ValueError("Timeout must be between 1 and 3000 milliseconds.")
    if engine.dialect.name != "postgresql":
        raise AnalyticalQueryError("Analytical execution requires PostgreSQL.")
    bounded_sql = f"SELECT * FROM ({query.normalized_sql}) AS analytical_result LIMIT {max_rows + 1}"
    try:
        with engine.connect().execution_options(postgresql_readonly=True) as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
                connection.exec_driver_sql("SET LOCAL search_path = pg_catalog")
                result = connection.exec_driver_sql(bounded_sql)
                try:
                    columns = list(result.keys())
                    fetched = result.fetchmany(max_rows + 1)
                finally:
                    result.close()
                rows = [list(row) for row in fetched[:max_rows]]
                return AnalyticalResult(
                    columns=columns, rows=rows, returned_row_count=len(rows),
                    truncated=len(fetched) > max_rows,
                )
            finally:
                transaction.rollback()
    except SQLAlchemyError as error:
        if getattr(getattr(error, "orig", None), "sqlstate", None) == "57014":
            raise AnalyticalQueryTimeout("Analytical query exceeded its execution deadline.") from None
        raise AnalyticalQueryError("Analytical query failed; check database availability and SQL validity.") from None
