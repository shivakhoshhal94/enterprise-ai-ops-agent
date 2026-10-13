"""One explicit action with atomic audit creation and race-safe idempotency."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from enterprise_ai_ops_agent.db.models import Order
from enterprise_ai_ops_agent.operations.contracts import CreateOrderReviewCaseRequest, OrderReviewCase
from enterprise_ai_ops_agent.operations.models import CaseEvent, OrderReviewCase as CaseModel


class ActionError(RuntimeError):
    """Safe operational error; no database details are exposed."""


class UnknownOrderError(ActionError):
    pass


class IdempotencyConflict(ActionError):
    pass


def _existing_case(connection, request: CreateOrderReviewCaseRequest) -> OrderReviewCase | None:
    row = connection.execute(select(CaseModel.__table__).where(
        CaseModel.idempotency_key == request.idempotency_key
    )).mappings().first()
    if row is None:
        return None
    for field in ("order_id", "reason", "priority"):
        if row[field] != getattr(request, field):
            raise IdempotencyConflict("Idempotency key already belongs to a different request.")
    return OrderReviewCase.model_validate(dict(row))


def create_order_review_case(
    engine: Engine, request: CreateOrderReviewCaseRequest, *, actor: str,
) -> OrderReviewCase:
    """Own and commit one transaction. Actor is supplied by trusted application code.

    Logical identity is order_id/reason/priority, exactly as provided. Actor and
    generated metadata are not part of retry identity; the original actor remains.
    """
    if not isinstance(request, CreateOrderReviewCaseRequest):
        raise ActionError("A validated CreateOrderReviewCaseRequest is required.")
    # Revalidate even objects constructed through Pydantic's unchecked escape hatch.
    try:
        request = CreateOrderReviewCaseRequest.model_validate(request.model_dump())
    except ValueError:
        raise ActionError("Invalid review-case request.") from None
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ActionError("Invalid application actor.")
    try:
        with engine.begin() as connection:
            existing = _existing_case(connection, request)
            if existing is not None:
                return existing
            if connection.execute(select(Order.order_id).where(
                Order.order_id == request.order_id
            )).scalar_one_or_none() is None:
                raise UnknownOrderError("Referenced historical order does not exist.")
            result = OrderReviewCase(
                **request.model_dump(), case_id=uuid4(), status="open",
                created_at=datetime.now(timezone.utc), created_by=actor,
            )
            try:
                # Savepoint isolates a competing unique-key insert without aborting
                # the outer transaction; PostgreSQL READ COMMITTED sees its winner.
                with connection.begin_nested():
                    connection.execute(insert(CaseModel), result.model_dump())
            except IntegrityError:
                existing = _existing_case(connection, request)
                if existing is None:
                    raise ActionError("Review-case insertion failed.") from None
                return existing
            connection.execute(insert(CaseEvent), {
                "event_id": uuid4(), "case_id": result.case_id,
                "event_type": "case_created", "actor": actor,
                "created_at": result.created_at,
            })
        return result
    except SQLAlchemyError:
        raise ActionError("Review-case transaction failed; no partial action was committed.") from None
