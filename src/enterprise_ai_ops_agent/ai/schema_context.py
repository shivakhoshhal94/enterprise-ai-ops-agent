"""Structural schema context derived offline from the application metadata."""

from sqlalchemy.dialects import postgresql

from enterprise_ai_ops_agent.db import models  # Register the existing tables.
from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.analytics.validator import ALLOWED_TABLES


def analytical_schema_context() -> str:
    lines = ["PostgreSQL application schema (only these public tables are available):"]
    for name in sorted(ALLOWED_TABLES):
        table = Base.metadata.tables[name]
        columns = []
        for column in table.columns:
            details = str(column.type.compile(dialect=postgresql.dialect()))
            details += " NULL" if column.nullable else " NOT NULL"
            columns.append(f"{column.name} {details}")
        lines.append(f"{name}({', '.join(columns)})")
        lines.append("  PK: " + ", ".join(column.name for column in table.primary_key))
        for foreign_key in sorted(table.foreign_keys, key=lambda key: key.parent.name):
            lines.append(f"  FK: {foreign_key.parent.name} -> {foreign_key.target_fullname}")
    return "\n".join(lines)
