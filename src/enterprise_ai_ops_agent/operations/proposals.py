"""Ephemeral intent representation. No operational tables or write service imports."""

from uuid import uuid4

from sqlalchemy import Connection, select

from enterprise_ai_ops_agent.db.models import Order
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseIntent, OrderReviewCaseProposal


class ProposalValidationError(ValueError):
    """Safe proposal rejection without unrelated order information."""


def propose_order_review_case(
    connection: Connection, intent: OrderReviewCaseIntent,
) -> OrderReviewCaseProposal:
    """Verify the reference with one SELECT and generate an in-memory proposal."""
    if not isinstance(intent, OrderReviewCaseIntent):
        raise ProposalValidationError("A validated order-review intent is required.")
    try:
        intent = OrderReviewCaseIntent.model_validate(intent.model_dump())
    except ValueError:
        raise ProposalValidationError("Invalid order-review intent.") from None
    if connection.execute(select(Order.order_id).where(
        Order.order_id == intent.order_id
    )).scalar_one_or_none() is None:
        raise ProposalValidationError("Cannot propose review: referenced historical order does not exist.")
    return OrderReviewCaseProposal(**intent.model_dump(), proposal_id=uuid4())
