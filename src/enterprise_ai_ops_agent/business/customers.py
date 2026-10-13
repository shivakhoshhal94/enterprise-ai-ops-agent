"""Repeat-customer history across order-associated customer records."""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import Connection, select

from enterprise_ai_ops_agent.db.models import Customer, Order


class CustomerOrder(BaseModel):
    order_id: str
    order_status: str
    order_purchase_timestamp: datetime


class CustomerOrderHistory(BaseModel):
    customer_unique_id: str
    found: bool
    customer_record_count: int
    order_count: int
    orders: list[CustomerOrder]


def get_customer_order_history(connection: Connection, customer_unique_id: str) -> CustomerOrderHistory:
    """Exact unique-identity lookup, sorted by purchase time then order ID."""
    if not customer_unique_id.strip():
        raise ValueError("customer_unique_id must be nonblank.")
    statement = (
        select(Customer.customer_id, Order.order_id, Order.order_status, Order.order_purchase_timestamp)
        .select_from(Customer)
        .outerjoin(Order, Order.customer_id == Customer.customer_id)
        .where(Customer.customer_unique_id == customer_unique_id)
        .order_by(Order.order_purchase_timestamp.asc(), Order.order_id.asc(), Customer.customer_id.asc())
    )
    rows = list(connection.execute(statement).mappings())
    records = {row["customer_id"] for row in rows}
    orders = [
        CustomerOrder(
            order_id=row["order_id"], order_status=row["order_status"],
            order_purchase_timestamp=row["order_purchase_timestamp"],
        )
        for row in rows if row["order_id"] is not None
    ]
    return CustomerOrderHistory(
        customer_unique_id=customer_unique_id, found=bool(records),
        customer_record_count=len(records), order_count=len(orders), orders=orders,
    )
