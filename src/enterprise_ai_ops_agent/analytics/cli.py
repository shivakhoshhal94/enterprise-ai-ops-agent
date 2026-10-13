"""Developer-only manual SQL command; no model, API endpoint, or schema creation."""

import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.analytics.executor import execute_analytical_query
from enterprise_ai_ops_agent.analytics.models import AnalyticalQueryError, SQLValidationError
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_analytics_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one guarded read-only analytical query.")
    parser.add_argument("sql")
    arguments = parser.parse_args(argv)
    try:
        settings = Settings()
        query = validate_analytical_sql(arguments.sql)
        engine = create_analytics_engine(settings)
    except SQLValidationError as error:
        print(f"SQL rejected: {error}", file=sys.stderr)
        return 1
    except (ValueError, SQLAlchemyError):
        print("Analytics configuration error: check ANALYTICS_DATABASE_URL.", file=sys.stderr)
        return 1
    try:
        result = execute_analytical_query(engine, query)
        print(result.model_dump_json())
        return 0
    except AnalyticalQueryError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
