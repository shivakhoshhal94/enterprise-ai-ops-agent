"""Small database operations shared by the explicit CSV loaders."""

from collections.abc import Mapping, Sequence

from sqlalchemy import Connection, Table, func, insert, select
from sqlalchemy.engine.reflection import Inspector


def require_empty_table(
    connection: Connection, table: Table, inspector: Inspector,
    label: str, error_type: type[RuntimeError],
) -> None:
    if not inspector.has_table(table.name, schema=table.schema):
        raise error_type(
            f"{label} table is missing; run python -m enterprise_ai_ops_agent.db.create_schema first."
        )
    if connection.execute(select(func.count()).select_from(table)).scalar_one() != 0:
        raise error_type(f"{label} table is not empty; refusing to append or replace rows.")


def insert_reconciled_rows(
    connection: Connection, table: Table, rows: Sequence[Mapping[str, object]],
    batch_size: int, error_type: type[RuntimeError],
) -> int:
    """Caller owns the transaction and checks the target is empty first."""
    if batch_size < 1:
        raise error_type("Batch size must be positive.")
    for start in range(0, len(rows), batch_size):
        connection.execute(insert(table), rows[start:start + batch_size])
    inserted = connection.execute(select(func.count()).select_from(table)).scalar_one()
    if inserted != len(rows):
        raise error_type("Inserted row count does not match the validated source; rolling back.")
    return inserted
