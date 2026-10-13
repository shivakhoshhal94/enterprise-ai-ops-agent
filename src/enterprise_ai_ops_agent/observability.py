"""Content-free application events. No provider objects or exception text are logged."""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import math
from time import perf_counter

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
LOGGER = logging.getLogger("enterprise_ai_ops_agent.events")
TOOLS = frozenset({"get_order_status_summary", "get_customer_order_history",
    "get_order_financial_summary", "run_analytical_query", "propose_order_review_case",
    "search_company_knowledge", "get_support_context"})
SECRET_KEYS = ("password", "secret", "token", "apikey", "authorization", "credential", "databaseurl")


def redact(value, depth=0):
    """Bounded defensive normalization; unsupported objects are never repr/str'ed."""
    try:
        if depth > 8:
            return "[truncated]"
        if isinstance(value, Mapping):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 64:
                    break
                if not isinstance(key, str):
                    continue
                normalized = "".join(c for c in key.lower() if c.isalnum())
                result[key[:128]] = ("[redacted]" if any(s in normalized for s in SECRET_KEYS)
                                    else redact(item, depth + 1))
            return result
        if isinstance(value, (list, tuple)):
            return [redact(item, depth + 1) for item in value[:64]]
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if type(value) is str:
            return value[:256]
        return "[unsupported]"
    except Exception:
        return "[unavailable]"


class EventFormatter(logging.Formatter):
    def format(self, record):
        # Only our explicit structured event envelope is accepted, never record.msg.
        return json.dumps(redact(getattr(record, "event_fields", {})), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def configure_logging(level="INFO"):
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(EventFormatter())
        LOGGER.addHandler(handler)
    LOGGER.setLevel(level)


def error_category(error, component=None):
    """Classify types/codes only, without rendering an exception."""
    try:
        category = getattr(error, "operational_category", None)
        if category in {"configuration_error","validation_error","database_unavailable","analytics_unavailable",
                         "knowledge_unavailable","provider_unavailable","support_system_unavailable",
                         "workflow_unavailable","timeout","internal_error"}:
            return category
        return _classify_error(error, component)
    except Exception:
        return "internal_error"


def _classify_error(error, component=None):
    names = {cls.__name__ for cls in type(error).__mro__}
    if "TimeoutError" in names or "APITimeoutError" in names or getattr(error, "code", None) == "timeout":
        return "timeout"
    if names & {"ValidationError", "SQLValidationError", "InvalidSupportRequest", "InvalidApprovalError", "InvalidWorkflowIdError", "ProposalValidationError"}:
        return "validation_error"
    if names & {"AssistantSupportError", "SupportToolError", "SupportIntegrationError"}:
        return "support_system_unavailable"
    if names & {"AssistantKnowledgeError", "KnowledgeToolError", "KnowledgeError"}:
        return "knowledge_unavailable"
    if "WorkflowError" in names:
        return "workflow_unavailable"
    if names & {"AnalyticalQueryError", "AnalyticalProposalError"}:
        return "analytics_unavailable"
    if "ValueError" in names:
        return "configuration_error"
    if names & {"SQLAlchemyError", "ToolExecutionError", "AssistantToolError"}:
        return "database_unavailable"
    if names & {"OpenAIError", "AssistantProviderError", "AssistantError"}:
        return "provider_unavailable"
    return {"provider":"provider_unavailable", "workflow":"workflow_unavailable",
            "analytics":"analytics_unavailable", "knowledge":"knowledge_unavailable",
            "support":"support_system_unavailable", "database":"database_unavailable"}.get(component, "internal_error")


def emit(event: str, component: str, **fields):
    """Only known operational fields enter logs; logging failures never escape."""
    try:
        allowed = {"duration_ms", "outcome", "error_category", "method", "route", "status_code",
                   "tool_name", "tool_names", "tool_count", "has_knowledge_sources", "has_external_sources",
                   "has_action_proposal", "has_workflow"}
        envelope = {"timestamp":datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "level":"INFO", "event":event, "component":component, "request_id":request_id.get()}
        envelope.update({key:value for key,value in fields.items() if key in allowed})
        LOGGER.info("", extra={"event_fields":redact(envelope)})
    except Exception:
        pass


@contextmanager
def timed(component):
    start = perf_counter()
    emit("dependency_started", component)
    try:
        yield
    except BaseException as error:
        emit("dependency_failed", component, duration_ms=round((perf_counter()-start)*1000, 3),
             outcome="failed", error_category=error_category(error, component))
        raise
    else:
        emit("dependency_completed", component, duration_ms=round((perf_counter()-start)*1000, 3), outcome="completed")


def observe_assistant(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        start = perf_counter()
        emit("assistant_request_started", "assistant")
        try:
            result = function(*args, **kwargs)
        except BaseException as error:
            emit("assistant_request_failed", "assistant", outcome="failed", error_category=error_category(error),
                 duration_ms=round((perf_counter()-start)*1000, 3))
            raise
        names = [call.tool_name for call in result.tool_calls if call.tool_name in TOOLS]
        emit("assistant_request_completed", "assistant", outcome="completed",
             duration_ms=round((perf_counter()-start)*1000, 3), tool_names=names, tool_count=len(result.tool_calls),
             has_knowledge_sources=bool(result.knowledge_sources), has_external_sources=bool(result.external_sources),
             has_action_proposal=result.action_proposal is not None, has_workflow=result.workflow is not None)
        return result
    return wrapped


def observe_workflow(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with timed("workflow"):
            return function(*args, **kwargs)
    return wrapped
