"""JSON-only checkpoint state and validated external human decisions."""

from typing import Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from enterprise_ai_ops_agent.operations.contracts import OrderReviewCase, OrderReviewCaseProposal

WorkflowStatus = Literal["awaiting_approval", "approved", "executing", "rejected", "completed", "failed"]


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, max_length=80)

    @field_validator("actor")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Approval actor must be nonblank.")
        return value


class ProposalData(TypedDict):
    proposal_id: str
    action_type: Literal["create_order_review_case"]
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    requires_approval: Literal[True]


class DecisionData(TypedDict):
    decision: Literal["approve", "reject"]
    actor: str


class CaseData(TypedDict):
    case_id: str
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    status: Literal["open"]
    idempotency_key: str
    created_at: str
    created_by: str


class WorkflowInput(TypedDict):
    workflow_id: str
    proposal: ProposalData


class WorkflowState(WorkflowInput):
    status: WorkflowStatus
    approval: DecisionData | None
    case: CaseData | None
    error: str | None


class OrderReviewWorkflowState(BaseModel):
    """Typed public projection; graph state itself stores plain JSON values."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    proposal: OrderReviewCaseProposal
    status: WorkflowStatus
    approval: ApprovalDecision | None = None
    case: OrderReviewCase | None = None
    error: str | None = None
    approval_request: dict[str, str | bool] | None = None


class WorkflowError(RuntimeError):
    """Safe internal workflow boundary failure without credentials or driver details."""


class InvalidWorkflowIdError(WorkflowError):
    pass


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowStateConflictError(WorkflowError):
    pass


class WorkflowSummary(BaseModel):
    """Public projection; never exposes case metadata or checkpoint internals."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: UUID
    proposal_id: UUID
    status: WorkflowStatus
    action_type: Literal["create_order_review_case"]
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    case_id: UUID | None = None
    failure_category: Literal["action_execution_failed"] | None = None

    @classmethod
    def from_state(cls, state: OrderReviewWorkflowState) -> "WorkflowSummary":
        return cls(
            workflow_id=state.workflow_id, proposal_id=state.proposal.proposal_id,
            status=state.status, action_type=state.proposal.action_type,
            order_id=state.proposal.order_id, reason=state.proposal.reason,
            priority=state.proposal.priority,
            case_id=state.case.case_id if state.status == "completed" and state.case else None,
            failure_category="action_execution_failed" if state.status == "failed" else None,
        )


class InvalidApprovalError(WorkflowError):
    pass
