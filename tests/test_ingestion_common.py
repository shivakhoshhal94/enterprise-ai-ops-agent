from unittest.mock import MagicMock

import pytest
from sqlalchemy.sql.dml import Insert

from enterprise_ai_ops_agent.db.models import CategoryTranslation
from enterprise_ai_ops_agent.ingestion.common import insert_reconciled_rows, require_empty_table


@pytest.mark.parametrize("exists,count,message", [(False, 0, "missing"), (True, 1, "not empty")])
def test_target_protection(exists: bool, count: int, message: str) -> None:
    connection, inspector = MagicMock(), MagicMock()
    inspector.has_table.return_value = exists
    connection.execute.return_value.scalar_one.return_value = count
    with pytest.raises(RuntimeError, match=message):
        require_empty_table(connection, CategoryTranslation.__table__, inspector, "Category translations", RuntimeError)
    if not exists:
        connection.execute.assert_not_called()


def test_batch_tail_and_existing_model_table() -> None:
    connection = MagicMock()
    connection.execute.return_value.scalar_one.return_value = 3
    table = CategoryTranslation.__table__
    rows = [{"product_category_name": str(i), "product_category_name_english": str(i)} for i in range(3)]
    assert insert_reconciled_rows(connection, table, rows, 2, RuntimeError) == 3
    calls = connection.execute.call_args_list
    assert [len(c.args[1]) for c in calls if isinstance(c.args[0], Insert)] == [2, 1]
    assert all(c.args[0].table is table for c in calls if isinstance(c.args[0], Insert))


@pytest.mark.parametrize("size,count,message", [(0, 2, "positive"), (2, 1, "does not match")])
def test_invalid_batch_or_reconciliation(size: int, count: int, message: str) -> None:
    connection = MagicMock()
    connection.execute.return_value.scalar_one.return_value = count
    with pytest.raises(RuntimeError, match=message):
        insert_reconciled_rows(connection, CategoryTranslation.__table__, [{}, {}], size, RuntimeError)
