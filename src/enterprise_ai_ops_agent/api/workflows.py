"""Narrow authenticated HTTP handoff; no graph internals or direct action calls."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from secrets import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from enterprise_ai_ops_agent.workflows.contracts import (
    ApprovalDecision, InvalidWorkflowIdError, WorkflowNotFoundError,
    WorkflowStateConflictError, WorkflowSummary,
)
from enterprise_ai_ops_agent.workflows.persistence import postgres_workflow
from enterprise_ai_ops_agent.workflows.service import OrderReviewWorkflow

router = APIRouter(prefix="/workflows", tags=["workflows"])
approval_header = APIKeyHeader(name="X-Approval-Key", scheme_name="ApprovalKey", auto_error=False)
WorkflowFactory = Callable[[], AbstractContextManager[OrderReviewWorkflow]]


def require_approval_access(
    request: Request, supplied: Annotated[str | None, Depends(approval_header)],
) -> None:
    secret = request.app.state.settings.approval_api_key
    if secret is None or not secret.get_secret_value().strip():
        raise HTTPException(status_code=503, detail="Workflow approval access is not configured.")
    if supplied is None or not compare_digest(supplied.encode("utf-8"), secret.get_secret_value().encode("utf-8")):
        raise HTTPException(status_code=401, detail="Workflow access denied.")


async def require_no_action_payload(
    request: Request, access: Annotated[None, Depends(require_approval_access)],
) -> None:
    # No caller-controlled actor, decision, proposal edits or resume payload.
    raw = await request.body()
    if not raw.strip():
        return
    try:
        empty = json.loads(raw) == {}
    except (ValueError, UnicodeDecodeError):
        empty = False
    if not empty:
        raise HTTPException(status_code=422, detail="Approval routes accept no caller-provided action fields.")


def get_workflow_factory(request: Request) -> WorkflowFactory:
    settings = request.app.state.settings
    return lambda: postgres_workflow(settings)  # Lazy; never runs setup or memory fallback.


def workflow_http_error(error: Exception) -> HTTPException:
    if isinstance(error, InvalidWorkflowIdError):
        return HTTPException(status_code=400, detail="Invalid workflow ID.")
    if isinstance(error, WorkflowNotFoundError):
        return HTTPException(status_code=404, detail="Workflow not found.")
    if isinstance(error, WorkflowStateConflictError):
        return HTTPException(status_code=409, detail="Workflow decision conflicts with its terminal or current state.")
    return HTTPException(status_code=503, detail="Workflow service is unavailable.")


def get_protected_workflow(
    access: Annotated[None, Depends(require_approval_access)],
    factory: Annotated[WorkflowFactory, Depends(get_workflow_factory)],
):
    try:
        with factory() as workflow:
            yield workflow
    except HTTPException:
        raise
    except Exception as error:
        raise workflow_http_error(error) from None


ProtectedWorkflow = Annotated[OrderReviewWorkflow, Depends(get_protected_workflow)]


@router.get("/{workflow_id}", response_model=WorkflowSummary)
def status(workflow_id: str, workflow: ProtectedWorkflow) -> WorkflowSummary:
    return WorkflowSummary.from_state(workflow.get_state(workflow_id))


@router.post("/{workflow_id}/approve", response_model=WorkflowSummary,
             dependencies=[Depends(require_no_action_payload)])
def approve(workflow_id: str, workflow: ProtectedWorkflow) -> WorkflowSummary:
    return WorkflowSummary.from_state(workflow.resume(
        workflow_id, ApprovalDecision(decision="approve", actor="approval_api"),
    ))


@router.post("/{workflow_id}/reject", response_model=WorkflowSummary,
             dependencies=[Depends(require_no_action_payload)])
def reject(workflow_id: str, workflow: ProtectedWorkflow) -> WorkflowSummary:
    return WorkflowSummary.from_state(workflow.resume(
        workflow_id, ApprovalDecision(decision="reject", actor="approval_api"),
    ))
