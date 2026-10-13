"""Small trusted application boundary; callers never submit graph state updates."""

import json
from contextlib import contextmanager, nullcontext
from threading import RLock
from uuid import UUID, uuid4

from langgraph.types import Command
from pydantic import ValidationError

from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.workflows.contracts import (
    ApprovalDecision, InvalidApprovalError, OrderReviewWorkflowState, WorkflowError,
    InvalidWorkflowIdError, WorkflowNotFoundError, WorkflowStateConflictError,
)
from enterprise_ai_ops_agent.workflows.graph import build_order_review_graph
from enterprise_ai_ops_agent.observability import observe_workflow


def thread_config(workflow_id: str) -> dict:
    try:
        identity = str(UUID(workflow_id))
    except (ValueError, TypeError, AttributeError):
        raise InvalidWorkflowIdError("Workflow ID must be a UUID.") from None
    return {"configurable": {"thread_id": identity}}


class OrderReviewWorkflow:
    def __init__(self, *, engine, checkpointer, lock=None):
        self._graph = build_order_review_graph(engine=engine, checkpointer=checkpointer)
        self._local_lock = RLock()
        external_lock = lock or (lambda workflow_id: nullcontext())
        @contextmanager
        def serialized(workflow_id):
            # Session advisory locks are reentrant on one PostgreSQL connection;
            # also serialize callers sharing this wrapper inside a process.
            with self._local_lock, external_lock(workflow_id):
                yield
        self._lock = serialized

    def _snapshot(self, workflow_id):
        return self._graph.get_state(thread_config(workflow_id))

    def _view(self, workflow_id) -> OrderReviewWorkflowState:
        snapshot = self._snapshot(workflow_id)
        if not snapshot.values:
            raise WorkflowNotFoundError("Order-review workflow does not exist.")
        values = dict(snapshot.values)
        if values["workflow_id"] != str(UUID(workflow_id)):
            raise WorkflowError("Workflow identity does not match its thread.")
        pending = [item.value for task in snapshot.tasks for item in task.interrupts]
        values["approval_request"] = pending[0] if pending else None
        return OrderReviewWorkflowState.model_validate_json(json.dumps(values))

    @observe_workflow
    def start(self, proposal: OrderReviewCaseProposal) -> OrderReviewWorkflowState:
        if not isinstance(proposal, OrderReviewCaseProposal):
            raise WorkflowError("A validated order-review proposal is required; initial decisions are not accepted.")
        try:
            proposal = OrderReviewCaseProposal.model_validate_json(proposal.model_dump_json())
            workflow_id = str(uuid4())
            with self._lock(workflow_id):
                if self._snapshot(workflow_id).values:
                    raise WorkflowError("Generated workflow identity already exists; proposal cannot be replaced.")
                self._graph.invoke({"workflow_id": workflow_id, "proposal": proposal.model_dump(mode="json")},
                                   thread_config(workflow_id), durability="sync")
                return self._view(workflow_id)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("Workflow start failed; check persistence and proposal configuration.") from None

    @observe_workflow
    def get_state(self, workflow_id: str) -> OrderReviewWorkflowState:
        workflow_id = thread_config(workflow_id)["configurable"]["thread_id"]
        try:
            with self._lock(workflow_id):
                return self._view(workflow_id)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("Workflow state could not be loaded safely.") from None

    @observe_workflow
    def resume(self, workflow_id: str, decision: ApprovalDecision) -> OrderReviewWorkflowState:
        if not isinstance(decision, ApprovalDecision):
            raise InvalidApprovalError("A validated external approval decision is required.")
        try:
            decision = ApprovalDecision.model_validate(decision.model_dump())
        except ValidationError:
            raise InvalidApprovalError("Invalid approval decision or actor.") from None
        workflow_id = thread_config(workflow_id)["configurable"]["thread_id"]
        try:
            with self._lock(workflow_id):
                current = self._view(workflow_id)
                if current.status == "completed" and decision.decision == "approve":
                    return current  # Duplicate approval cannot invoke action again.
                if current.status != "awaiting_approval" or current.approval_request is None:
                    raise WorkflowStateConflictError("Workflow is not awaiting approval; rejection is terminal.")
                self._graph.invoke(Command(resume=decision.model_dump()), thread_config(workflow_id), durability="sync")
                return self._view(workflow_id)
        except WorkflowError:
            raise
        except ValidationError:
            raise InvalidApprovalError("Invalid approval decision or actor.") from None
        except Exception:
            raise WorkflowError("Workflow resume failed; reload state before retrying.") from None

    @observe_workflow
    def retry(self, workflow_id: str) -> OrderReviewWorkflowState:
        """Continue already approved work after failure/crash; never bypass approval."""
        workflow_id = thread_config(workflow_id)["configurable"]["thread_id"]
        try:
            with self._lock(workflow_id):
                current = self._view(workflow_id)
                if current.status == "completed":
                    return current
                if (current.status not in ("approved", "executing", "failed")
                        or current.approval is None or current.approval.decision != "approve"):
                    raise WorkflowError("Only an already approved interrupted/failed action may be retried.")
                config = thread_config(workflow_id)
                if not self._snapshot(workflow_id).next:
                    # Fixed application-owned retry edge; proposal/decision are immutable.
                    self._graph.update_state(config, {"status": "executing", "error": None}, as_node="mark_executing")
                self._graph.invoke(None, config, durability="sync")
                return self._view(workflow_id)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("Workflow retry failed; reload state before retrying.") from None
