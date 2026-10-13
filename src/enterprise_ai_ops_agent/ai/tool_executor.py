"""Allowlisted dispatch to application-owned SELECT functions, never model SQL."""

import json
import anyio
from time import perf_counter
from enterprise_ai_ops_agent.observability import emit, error_category, timed, TOOLS

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine

from enterprise_ai_ops_agent.business.customers import get_customer_order_history
from enterprise_ai_ops_agent.business.finance import get_order_financial_summary
from enterprise_ai_ops_agent.business.orders import get_order_status_summary
from enterprise_ai_ops_agent.ai.tool_schemas import ARGUMENT_MODELS
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql
from enterprise_ai_ops_agent.analytics.executor import execute_configured_analytical_query
from enterprise_ai_ops_agent.analytics.models import SQLValidationError, AnalyticalQueryError
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseIntent, OrderReviewCaseProposal
from enterprise_ai_ops_agent.operations.proposals import ProposalValidationError, propose_order_review_case
from enterprise_ai_ops_agent.ai.knowledge_contracts import KnowledgeSource, evidence_sources
from enterprise_ai_ops_agent.ai.knowledge_tool import retrieve_evidence
from enterprise_ai_ops_agent.integrations.support_tickets.client import get_support_context, SupportIntegrationError
from enterprise_ai_ops_agent.integrations.support_tickets.contracts import SupportContext
from enterprise_ai_ops_agent.ai.support_contracts import ExternalSource, external_source


HANDLERS = {
    "get_order_status_summary": get_order_status_summary,
    "get_customer_order_history": get_customer_order_history,
    "get_order_financial_summary": get_order_financial_summary,
}


class ToolExecutionError(RuntimeError):
    """Credential-safe failure without driver or argument contents."""


class AnalyticalProposalError(ToolExecutionError):
    """A sanitized rejection eligible for at most one assistant correction."""


class KnowledgeToolError(ToolExecutionError):
    """Knowledge unavailable; unrelated tools remain usable."""


class SupportToolError(ToolExecutionError):
    def __init__(self, message: str, *, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


class ToolCallTrace(BaseModel):
    tool_name: str
    arguments: dict[str, str]


class ToolExecution(BaseModel):
    trace: ToolCallTrace
    output_json: str
    action_proposal: OrderReviewCaseProposal | None = None
    knowledge_sources: list[KnowledgeSource] = Field(default_factory=list)
    external_sources: list[ExternalSource] = Field(default_factory=list)


def execute_tool(engine: Engine, name: str, arguments_json: str, *, settings: Settings | None = None, client=None) -> ToolExecution:
    safe_name = name if name in TOOLS else "unsupported"
    component = {"run_analytical_query":"analytics", "search_company_knowledge":"knowledge",
                 "get_support_context":"support"}.get(safe_name, "database")
    start = perf_counter()
    emit("tool_execution_started", "tool", tool_name=safe_name)
    try:
        with timed(component):
            result = _execute_tool(engine, name, arguments_json, settings=settings, client=client)
    except BaseException as error:
        emit("tool_execution_failed", "tool", tool_name=safe_name, outcome="failed",
             duration_ms=round((perf_counter()-start)*1000,3), error_category=error_category(error, component))
        raise
    emit("tool_execution_completed", "tool", tool_name=safe_name, outcome="completed",
         duration_ms=round((perf_counter()-start)*1000,3))
    return result


def _execute_tool(engine: Engine, name: str, arguments_json: str, *, settings: Settings | None = None, client=None) -> ToolExecution:
    if name not in ARGUMENT_MODELS:
        raise ToolExecutionError("Unsupported tool requested.")
    try:
        arguments = ARGUMENT_MODELS[name].model_validate_json(arguments_json).model_dump()
    except (ValidationError, ValueError, TypeError):
        if name == "run_analytical_query":
            raise AnalyticalProposalError("Analytical proposal rejected: provide only a nonblank SQL string.") from None
        raise ToolExecutionError("Invalid tool arguments.") from None
    if name == "get_support_context":
        try:
            # The assistant is synchronous; /ask runs it in FastAPI's worker thread.
            context = anyio.run(get_support_context, arguments["order_id"])
            context = SupportContext.model_validate(context)
            if context.order_id != arguments["order_id"]:
                raise ValueError("Mismatched external identity.")
        except SupportIntegrationError as error:
            code = error.code
            message = "Support service timed out." if code == "timeout" else "Support service is unavailable or returned an invalid response."
            raise SupportToolError(message, code=code) from None
        except Exception:
            raise SupportToolError("Support service returned an invalid response.", code="invalid_response") from None
        return ToolExecution(trace=ToolCallTrace(tool_name=name, arguments=arguments),
                             output_json=context.model_dump_json(), external_sources=[external_source(context)])
    if name == "search_company_knowledge":
        if settings is None or client is None:
            raise KnowledgeToolError("Company knowledge service is not configured.")
        try:
            result = retrieve_evidence(arguments["query"], settings=settings, client=client)
        except Exception:
            raise KnowledgeToolError("Company knowledge service is unavailable.") from None
        return ToolExecution(trace=ToolCallTrace(tool_name=name, arguments=arguments),
                             output_json=result.model_dump_json(), knowledge_sources=evidence_sources(result))
    if name == "propose_order_review_case":
        try:
            with engine.connect().execution_options(postgresql_readonly=True) as connection:
                proposal = propose_order_review_case(connection, OrderReviewCaseIntent(**arguments))
        except ProposalValidationError as error:
            raise ToolExecutionError(str(error)) from None
        except Exception:
            raise ToolExecutionError("Order-review proposal lookup failed; check database access.") from None
        return ToolExecution(
            trace=ToolCallTrace(tool_name=name, arguments=arguments),
            output_json=proposal.model_dump_json(), action_proposal=proposal,
        )
    if name == "run_analytical_query":
        try:
            query = validate_analytical_sql(arguments["sql"])
        except SQLValidationError:
            raise AnalyticalProposalError("Analytical proposal rejected by the read-only SQL policy.") from None
        if settings is None or settings.analytics_database_url is None or not settings.analytics_database_url.get_secret_value().strip():
            raise ToolExecutionError("Analytics service is not configured.")
        try:
            result = execute_configured_analytical_query(settings, query)
        except AnalyticalQueryError:
            raise AnalyticalProposalError("Analytical query could not be completed safely; review the query against the supplied schema.") from None
        except Exception:
            raise ToolExecutionError("Analytical tool execution failed.") from None
        return ToolExecution(
            trace=ToolCallTrace(tool_name=name, arguments=arguments),
            output_json=result.model_dump_json(),
        )
    try:
        # PostgreSQL enforces read-only for this transaction; no connection opens at import.
        with engine.connect().execution_options(postgresql_readonly=True) as connection:
            result = HANDLERS[name](connection, **arguments)
        if isinstance(result, list):
            payload = [item.model_dump(mode="json") for item in result]
        else:
            payload = result.model_dump(mode="json") if result is not None else None
        output = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except Exception:
        # Driver and unexpected function exceptions can contain credentials or SQL details.
        raise ToolExecutionError("Business tool execution failed; check database access.") from None
    return ToolExecution(trace=ToolCallTrace(tool_name=name, arguments=arguments), output_json=output)
