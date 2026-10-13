"""Order status reporting without status reinterpretation."""

from pydantic import BaseModel
from sqlalchemy import Connection, func, select

from enterprise_ai_ops_agent.db.models import Order


class OrderStatusCount(BaseModel):
    order_status: str
    order_count: int


def get_order_status_summary(connection: Connection) -> list[OrderStatusCount]:
    """Return observed statuses in ascending lexical order; empty DB returns []."""
    statement = (
        select(Order.order_status, func.count().label("order_count"))
        .group_by(Order.order_status)
        .order_by(Order.order_status.asc())
    )
    return [OrderStatusCount.model_validate(row) for row in connection.execute(statement).mappings()]
