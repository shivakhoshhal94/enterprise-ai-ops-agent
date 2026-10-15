"""Bounded public projections of existing tool results; no execution or model access."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from enterprise_ai_ops_agent.business.customers import CustomerOrderHistory
from enterprise_ai_ops_agent.business.finance import OrderFinancialSummary
from enterprise_ai_ops_agent.business.orders import OrderStatusCount
from enterprise_ai_ops_agent.analytics.models import AnalyticalResult
from enterprise_ai_ops_agent.integrations.support_tickets.contracts import SupportContext

Cell = Annotated[str, Field(max_length=1000)] | int | float | bool | None
Tool = Literal["get_order_status_summary", "get_customer_order_history", "get_order_financial_summary", "run_analytical_query", "get_support_context"]


class PresentationData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    tool_name: Tool
    provenance: Literal["historical_database", "synthetic_support"]
    columns: list[Annotated[str, Field(max_length=128)]] = Field(max_length=32)
    rows: list[list[Cell]] = Field(max_length=200)
    summary: dict[str, Cell] = Field(default_factory=dict, max_length=20)
    truncated: bool = False


def project_tool_result(tool_name: str, output_json: str) -> PresentationData | None:
    """Fail closed for unsupported/oversized projections without affecting the tool loop."""
    try:
        payload = json.loads(output_json)
        summary = {}
        truncated = False
        if tool_name == "run_analytical_query":
            result = AnalyticalResult.model_validate(payload)
            columns, rows, truncated = result.columns, result.rows, result.truncated
        elif tool_name == "get_order_status_summary":
            columns = ["order_status", "order_count"]
            rows = [[r.order_status, r.order_count] for r in map(OrderStatusCount.model_validate, payload)]
        elif tool_name == "get_customer_order_history":
            result = CustomerOrderHistory.model_validate(payload)
            summary = {key: getattr(result, key) for key in ["customer_unique_id", "found", "customer_record_count", "order_count"]}
            columns = ["order_id", "order_status", "order_purchase_timestamp"]
            rows = [[r.order_id, r.order_status, r.order_purchase_timestamp.isoformat()] for r in result.orders[:200]]
            truncated = len(result.orders) > 200
        elif tool_name == "get_order_financial_summary" and payload is not None:
            result = OrderFinancialSummary.model_validate(payload)
            summary = result.model_dump(mode="json")
            columns, rows = [], []
        elif tool_name == "get_support_context":
            result = SupportContext.model_validate(payload)
            columns = ["ticket_id", "status", "category", "priority", "summary", "updated_at"]
            rows = [[getattr(ticket, key) for key in columns] for ticket in result.tickets]
            summary = {"order_id": result.order_id, "data_origin": result.data_origin}
            truncated = result.truncated
        else:
            return None
        if len(set(columns)) != len(columns) or any(len(row) != len(columns) for row in rows):
            return None
        return PresentationData(tool_name=tool_name, provenance="synthetic_support" if tool_name == "get_support_context" else "historical_database", columns=columns, rows=rows, summary=summary, truncated=truncated)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
