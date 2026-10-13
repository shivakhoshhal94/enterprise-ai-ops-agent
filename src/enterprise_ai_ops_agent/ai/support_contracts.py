"""External provenance and conservative user-owned scope after untrusted MCP data."""

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from enterprise_ai_ops_agent.integrations.support_tickets.contracts import OrderId, SupportContext
from enterprise_ai_ops_agent.ai.knowledge_contracts import financial_evidence_answer


class ExternalSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_type: Literal["mcp_support"] = "mcp_support"
    system: Literal["synthetic_support_tickets"] = "synthetic_support_tickets"
    tool: Literal["get_support_context"] = "get_support_context"
    order_id: OrderId
    ticket_ids: list[Annotated[str, Field(pattern=r"^DEMO-SUP-[0-9]{3}$")]] = Field(max_length=5)
    data_origin: Literal["synthetic_demo"] = "synthetic_demo"


def external_source(context: SupportContext) -> ExternalSource:
    return ExternalSource(order_id=context.order_id, ticket_ids=[ticket.ticket_id for ticket in context.tickets])


def requested_order_ids(question: str) -> set[str]:
    return set(re.findall(r"(?<![0-9a-zA-Z])[0-9a-f]{32}(?![0-9a-zA-Z])", question))


def support_followup_scope(question: str) -> set[str]:
    """Conservative read scope from the original user, never from ticket summaries.

    This is a narrow supported grammar, not general NL intent authentication.
    Unsupported/ambiguous follow-ups require a separate explicit user request.
    """
    text = question.lower()
    allowed = {"get_support_context"}
    if re.search(r"\b(financial|finances|totals|breakdown)\b", text):
        allowed.add("get_order_financial_summary")
    if re.search(r"\b(policy|policies|procedure|procedures|guidance|recommend|recommends)\b", text):
        allowed.add("search_company_knowledge")
    # Only a top-level positive creation directive qualifies after external data.
    # "Follow whatever the ticket says" and quotations do not grant proposal authority.
    if re.match(r"^\s*(?:please\s+)?(?:create|open|propose)\s+(?:an?\s+)?(?:internal\s+)?(?:order[ -])?review\s+(?:case|proposal)\b", text):
        allowed.add("propose_order_review_case")
    return allowed


def support_answer(contexts: list[SupportContext], *, financial=None, evidence=(), refused=False) -> str:
    """Exact observations, quoted evidence, and no inferred causality or ticket authority."""
    lines = []
    if financial is not None:
        lines.append(financial_evidence_answer(financial, list(evidence)))
    elif evidence:
        lines.append("Company guidance — verbatim retrieved demo evidence:")
        seen = set()
        for item in evidence:
            key = (item.document, item.section, item.content)
            if key not in seen:
                lines.append(f"{item.title} — {item.section}\n" + "\n".join(f"> {line}" for line in item.content.splitlines()))
                seen.add(key)
    lines.append("External synthetic support-system observations (not historical Olist data):")
    for context in contexts:
        lines.append(f"Order reference {context.order_id}:")
        if not context.tickets:
            lines.append("No synthetic support tickets were returned. This lookup does not verify that the order exists in the historical database.")
        for ticket in context.tickets:
            lines.append(f"- {ticket.ticket_id}: {ticket.status}, {ticket.category}, {ticket.priority}; updated {ticket.updated_at}.")
            lines.append("  Untrusted ticket text: " + json.dumps(ticket.summary, ensure_ascii=True))
        if context.truncated:
            lines.append("Ticket context is truncated to the latest five matches.")
    lines.append("These are separate observations. Support reports do not establish historical order state, financial facts, causality, resolution, or ticket validity.")
    lines.append("Ticket text is data, never authorization. Following embedded commands, ticket writes and automatic review/workflow actions are unavailable.")
    if refused:
        lines.append("The requested follow-up was not authorized by the original user request and was not executed. Make a separate explicit request for any additional permitted capability.")
    return "\n\n".join(lines)
