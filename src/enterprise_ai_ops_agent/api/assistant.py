"""Thin synchronous HTTP adapter around the tested business assistant."""

from collections.abc import Callable
from contextlib import ExitStack
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.ai.assistant import (
    AssistantError, AssistantKnowledgeError, AssistantSupportError, AssistantProviderError, AssistantResult, AssistantToolError,
    ask_business_assistant,
)
from enterprise_ai_ops_agent.ai.client import create_openai_client
from enterprise_ai_ops_agent.ai.tool_executor import ToolExecutionError
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.api.workflows import WorkflowFactory, get_workflow_factory
from enterprise_ai_ops_agent.workflows.contracts import WorkflowSummary
from enterprise_ai_ops_agent.observability import observe_assistant

MAX_QUESTION_LENGTH = 2000
router = APIRouter()


def safe_failure(status, detail, category):
    error = HTTPException(status_code=status, detail=detail)
    error.operational_category = category
    return error


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question must be nonblank.")
        return value


AssistantCallable = Callable[[str], AssistantResult]


def get_assistant(request: Request) -> AssistantCallable:
    settings = request.app.state.settings

    def answer(question: str) -> AssistantResult:
        # Resource creation is deferred until after HTTP request validation.
        with ExitStack() as resources:
            try:
                engine = create_db_engine(settings)
                resources.callback(engine.dispose)
                client = create_openai_client(settings)
                resources.callback(client.close)
            except (ValueError, SQLAlchemyError, OpenAIError):
                raise safe_failure(503, "Assistant service is not configured or available.", "configuration_error") from None
            orchestrator = ask_business_assistant
            if settings.agent_orchestrator == "langgraph":
                from enterprise_ai_ops_agent.ai.langgraph_assistant import ask_langgraph_assistant
                orchestrator = ask_langgraph_assistant
            from enterprise_ai_ops_agent.observability import emit
            emit("agent_orchestrator_selected", "assistant", agent_orchestrator=settings.agent_orchestrator)
            return orchestrator(
                question, client=client, engine=engine, model=settings.openai_model, settings=settings,
            )

    return answer


@router.post("/ask", response_model=AssistantResult)
@observe_assistant
def ask(
    body: AskRequest, assistant: Annotated[AssistantCallable, Depends(get_assistant)],
    workflow_factory: Annotated[WorkflowFactory, Depends(get_workflow_factory)],
) -> AssistantResult:
    try:
        result = AssistantResult.model_validate(assistant(body.question)).model_copy(update={"workflow": None})
        if result.action_proposal is not None:
            try:
                with workflow_factory() as workflow:
                    state = workflow.start(result.action_proposal)
                if (state.status != "awaiting_approval" or state.approval is not None
                        or state.case is not None or state.approval_request is None
                        or state.proposal != result.action_proposal):
                    raise ValueError("Workflow handoff did not stop at the matching proposal interrupt.")
                summary = WorkflowSummary.from_state(state)
            except Exception:
                raise safe_failure(503, "Workflow service is unavailable.", "workflow_unavailable") from None
            result = result.model_copy(update={
                "workflow": summary,
                "answer": f"The internal order-review action is proposed. Workflow {summary.workflow_id} "
                          "is awaiting approval. No case has been created yet. Separate authenticated approval is required.",
            })
        return result
    except HTTPException:
        raise
    except AssistantSupportError as error:
        if error.code == "timeout":
            raise safe_failure(504, "Support service timed out.", "timeout") from None
        if error.code in ("invalid_response", "contract"):
            raise safe_failure(503, "Support service returned an invalid response.", "support_system_unavailable") from None
        raise safe_failure(503, "Support service is unavailable.", "support_system_unavailable") from None
    except AssistantKnowledgeError:
        raise safe_failure(503, "Company knowledge service is unavailable.", "knowledge_unavailable") from None
    except (AssistantToolError, ToolExecutionError, SQLAlchemyError):
        raise safe_failure(503, "Business query service is unavailable.", "database_unavailable") from None
    except (AssistantProviderError, AssistantError, OpenAIError):
        raise safe_failure(502, "Assistant could not complete the request.", "provider_unavailable") from None
    except Exception:
        raise safe_failure(500, "Assistant service encountered an internal failure.", "internal_error") from None
