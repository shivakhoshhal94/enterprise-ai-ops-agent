"""Item and payment totals aggregated independently to avoid row multiplication."""

from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import Connection, func, select

from enterprise_ai_ops_agent.db.models import Order, OrderItem, OrderPayment


class OrderFinancialSummary(BaseModel):
    order_id: str
    item_count: int
    seller_count: int
    items_price_total: Decimal | None
    freight_total: Decimal | None
    items_and_freight_total: Decimal | None
    payment_row_count: int
    payment_total: Decimal | None
    difference: Decimal | None


def get_order_financial_summary(connection: Connection, order_id: str) -> OrderFinancialSummary | None:
    """Unknown order returns None; missing children have zero counts and NULL totals."""
    if not order_id.strip():
        raise ValueError("order_id must be nonblank.")
    items = (
        select(
            OrderItem.order_id,
            func.count().label("item_count"),
            func.count(func.distinct(OrderItem.seller_id)).label("seller_count"),
            func.sum(OrderItem.price).label("items_price_total"),
            func.sum(OrderItem.freight_value).label("freight_total"),
        )
        .where(OrderItem.order_id == order_id)
        .group_by(OrderItem.order_id)
        .subquery("item_totals")
    )
    payments = (
        select(
            OrderPayment.order_id,
            func.count().label("payment_row_count"),
            func.sum(OrderPayment.payment_value).label("payment_total"),
        )
        .where(OrderPayment.order_id == order_id)
        .group_by(OrderPayment.order_id)
        .subquery("payment_totals")
    )
    statement = (
        select(
            Order.order_id,
            func.coalesce(items.c.item_count, 0).label("item_count"),
            func.coalesce(items.c.seller_count, 0).label("seller_count"),
            items.c.items_price_total, items.c.freight_total,
            func.coalesce(payments.c.payment_row_count, 0).label("payment_row_count"),
            payments.c.payment_total,
        )
        .select_from(Order)
        .outerjoin(items, items.c.order_id == Order.order_id)
        .outerjoin(payments, payments.c.order_id == Order.order_id)
        .where(Order.order_id == order_id)
    )
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        return None
    combined = (
        row["items_price_total"] + row["freight_total"]
        if row["item_count"] else None
    )
    difference = (
        row["payment_total"] - combined
        if combined is not None and row["payment_total"] is not None else None
    )
    return OrderFinancialSummary(**row, items_and_freight_total=combined, difference=difference)
