"""Narrow caller input and application-owned operational outputs."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class OrderReviewCaseIntent(BaseModel):
    """Shared action-defining fields for proposals and execution requests."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    order_id: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=1000)
    priority: Literal["low", "medium", "high"]

    @field_validator("order_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must be nonblank.")
        return value


class CreateOrderReviewCaseRequest(OrderReviewCaseIntent):
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def nonblank_key(cls, value: str) -> str:
        return cls.nonblank(value)


class OrderReviewCaseProposal(OrderReviewCaseIntent):
    proposal_id: UUID
    action_type: Literal["create_order_review_case"] = "create_order_review_case"
    requires_approval: Literal[True] = True


class OrderReviewCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: UUID
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    status: Literal["open"]
    idempotency_key: str
    created_at: AwareDatetime
    created_by: str


class CaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    case_id: UUID
    event_type: Literal["case_created"]
    actor: str
    created_at: AwareDatetime
