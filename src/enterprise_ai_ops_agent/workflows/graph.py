"""One fixed state machine. Its only action node follows the human interrupt."""

import json
from uuid import UUID

from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt
from sqlalchemy import Engine

from enterprise_ai_ops_agent.operations.contracts import CreateOrderReviewCaseRequest, OrderReviewCaseProposal
from enterprise_ai_ops_agent.operations.service import create_order_review_case
from enterprise_ai_ops_agent.workflows.contracts import ApprovalDecision, WorkflowInput, WorkflowState, WorkflowError


def execution_idempotency_key(workflow_id: str) -> str:
    return f"workflow:{UUID(workflow_id)}:create_order_review_case"


def prepare_proposal(state: WorkflowState) -> dict:
    # Reset decision channels even if a raw internal invocation supplies extras.
    OrderReviewCaseProposal.model_validate_json(json.dumps(state["proposal"]))
    UUID(state["workflow_id"])
    return {"status": "awaiting_approval", "approval": None, "case": None, "error": None}


def await_human_approval(state: WorkflowState) -> dict:
    # Pure review payload. On resume this code runs again, without side effects.
    decision = interrupt({
        "workflow_id": state["workflow_id"], **state["proposal"],
        "effect": "Approval creates one internal order-review case and its audit event; no external action.",
    }, response_schema=ApprovalDecision)
    decision = ApprovalDecision.model_validate(decision)
    return {"approval": decision.model_dump(),
            "status": "approved" if decision.decision == "approve" else "rejected"}


def mark_executing(state: WorkflowState) -> dict:
    if state["approval"] is None or ApprovalDecision.model_validate(state["approval"]).decision != "approve":
        raise WorkflowError("Execution requires a validated external approval.")
    return {"status": "executing", "error": None}


def execute_case(state: WorkflowState, *, engine: Engine) -> dict:
    approval = ApprovalDecision.model_validate(state["approval"])
    if approval.decision != "approve" or state["status"] != "executing":
        raise WorkflowError("Execution requires the approved workflow branch.")
    proposal = OrderReviewCaseProposal.model_validate_json(json.dumps(state["proposal"]))
    request = CreateOrderReviewCaseRequest(
        order_id=proposal.order_id, reason=proposal.reason, priority=proposal.priority,
        idempotency_key=execution_idempotency_key(state["workflow_id"]),
    )
    try:
        result = create_order_review_case(engine, request, actor=f"approved_by:{approval.actor}")
    except Exception:
        # May include ambiguous success (business commit before a process error).
        # Never infer rollback here; retry the same deterministic idempotency key.
        return {"status": "failed", "error": "Action execution failed; retry this approved workflow safely.", "case": None}
    return {"case": result.model_dump(mode="json")}


def build_order_review_graph(*, engine: Engine, checkpointer):
    graph = StateGraph(WorkflowState, input_schema=WorkflowInput)
    graph.add_node("prepare_proposal", prepare_proposal)
    graph.add_node("await_human_approval", await_human_approval)
    graph.add_node("mark_rejected", lambda state: {"status": "rejected"})
    graph.add_node("mark_executing", mark_executing)
    graph.add_node("execute_case", lambda state: execute_case(state, engine=engine))
    graph.add_node("mark_completed", lambda state: {"status": "completed", "error": None})
    graph.add_edge(START, "prepare_proposal")
    graph.add_edge("prepare_proposal", "await_human_approval")
    graph.add_conditional_edges("await_human_approval", lambda state: state["approval"]["decision"],
                               {"approve": "mark_executing", "reject": "mark_rejected"})
    graph.add_edge("mark_rejected", END)
    graph.add_edge("mark_executing", "execute_case")
    graph.add_conditional_edges("execute_case", lambda state: "failed" if state["status"] == "failed" else "success",
                               {"failed": END, "success": "mark_completed"})
    graph.add_edge("mark_completed", END)
    return graph.compile(checkpointer=checkpointer)
