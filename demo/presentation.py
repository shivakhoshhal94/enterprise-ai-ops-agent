"""Display-only parsing. No queries, financial calculations or invented values."""

import re
from decimal import Decimal

from demo.api_client import AskResponse, Workflow

# Verified snapshot metrics from the repository's reconciliation records, not live KPIs.
VERIFIED_PROJECT_METRICS = {
    "Orders": "99,441", "Order Items": "112,650", "Payments": "103,886",
    "Assistant Tools": "7", "Knowledge Documents": "6", "Knowledge Chunks": "25",
}

STATUS_LABELS = {
    "awaiting_approval": "Awaiting Human Approval", "approved": "Approved",
    "executing": "Executing", "completed": "Completed", "rejected": "Rejected", "failed": "Failed",
}


def tool_names(result: AskResponse) -> list[str]:
    return list(dict.fromkeys(call.tool_name for call in result.tool_calls))


def canceled_count(result: AskResponse) -> int | None:
    if "get_order_status_summary" not in tool_names(result):
        return None
    number = r"(?<![\d,])((?:\d{1,3}(?:,\d{3})+|\d+))"
    matches = re.findall(number + r"\s+orders?(?:\s+that\s+were|\s+were|\s+are)?\s+cancel(?:ed|led)\b", result.answer, re.I)
    matches += re.findall(number + r"\s+cancel(?:ed|led)\s+orders?\b", result.answer, re.I)
    return int(matches[0].replace(",", "")) if len(matches) == 1 else None


def category_values(result: AskResponse) -> list[dict]:
    """Chart only a complete, unambiguous five-row format in the returned answer."""
    if "run_analytical_query" not in tool_names(result):
        return []
    text = result.answer.replace("**", "").replace("`", "")
    matches = re.findall(
        r"^\s*[1-5][.)]\s+([a-z][a-z0-9_]+)\s+with\s+a\s+total\s+item\s+price\s+of\s+"
        r"((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})\s+BRL\s*$", text, re.M,
    )
    matches += re.findall(
        r"^\s*[1-5][.)]\s+([a-z][a-z0-9_]+)\s*[-:–—]\s*BRL\s*"
        r"((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})\s*$", text, re.M,
    )
    if len(matches) != 5 or len({name for name, _ in matches}) != 5:
        return []
    return [{"Category": name, "Item value (BRL)": float(Decimal(value.replace(",", "")))}
            for name, value in matches]


def financial_lines(result: AskResponse) -> list[dict[str, str]]:
    if "get_order_financial_summary" not in tool_names(result):
        return []
    # Copy labeled lines verbatim: never calculate totals or parse unrelated ticket amounts.
    allowed = {"item price total", "freight total", "items plus freight", "recorded payment total", "recorded payment difference"}
    rows = []
    for line in result.answer.splitlines():
        match = re.fullmatch(r"\s*-\s*([^:]+):\s*(BRL\s+-?[\d,]+\.\d{2})\s*", line)
        if match and match[1].lower() in allowed:
            rows.append({"Financial fact": match[1], "Recorded value": match[2]})
    return rows if len(rows) == 5 and len({r["Financial fact"] for r in rows}) == 5 else []


def source_cards(result: AskResponse) -> tuple[list[dict], list[dict]]:
    return ([source.model_dump() for source in result.knowledge_sources],
            [source.model_dump() for source in result.external_sources])


def workflow_card(workflow: Workflow) -> dict:
    return {"label": STATUS_LABELS[workflow.status],
            "pending_without_action": workflow.status == "awaiting_approval" and workflow.case_id is None,
            "order_id": workflow.order_id, "priority": workflow.priority, "reason": workflow.reason}


CAPABILITY_LABELS = {
    "get_order_status_summary": "Business Data",
    "get_customer_order_history": "Customer History",
    "get_order_financial_summary": "Financial Data",
    "run_analytical_query": "Read-only Analytics",
    "search_company_knowledge": "Company Knowledge / RAG",
    "get_support_context": "External Support / MCP",
    "propose_order_review_case": "Review Proposal",
}


def capability_labels(result: AskResponse) -> list[str]:
    return [CAPABILITY_LABELS.get(name, "Other capability") for name in tool_names(result)]


def concise_answer(result: AskResponse) -> str:
    """Remove only the appended attribution footer, preserving the answer verbatim."""
    marker = "\n\nKnowledge evidence is application-owned demo documentation"
    return result.answer.partition(marker)[0].strip()


def compact_brl(value: float, *, millions: bool = False) -> str:
    """Presentation-only units; the exact returned amounts remain available."""
    amount = Decimal(str(value))
    if millions or abs(amount) >= 1_000_000:
        return f"BRL {amount / 1_000_000:.2f}M"
    if abs(amount) >= 1_000:
        return f"BRL {amount / 1_000:.2f}K"
    return f"BRL {amount:,.2f}"


def presentation_categories(result: AskResponse) -> list[dict]:
    """Validate complete currency rankings; never pick numbers out of arbitrary prose."""
    if "run_analytical_query" not in tool_names(result):
        return []
    text = result.answer.replace("**", "").replace("`", "")
    number = r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}"
    money = rf"(?:BRL\s+({number})|({number})\s+BRL)"
    ranked = []
    table = []
    table_currency = False
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d+[.)]\s", line):
            match = re.fullmatch(rf"(\d+)[.)]\s+([a-z][a-z0-9_]+)\s*(?:[-:–—]|with a total item price of)\s*{money}\.?", line)
            if not match:
                return []
            ranked.append((int(match[1]), match[2], match[3] or match[4]))
        elif line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(re.fullmatch(r"[:\s-]+", cell) for cell in cells):
                continue
            if len(cells) != 2:
                return []
            if cells[0].lower() in {"category", "product category", "product_category_name"}:
                table_currency = "BRL" in cells[1].upper()
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]+", cells[0]):
                return []
            match = re.fullmatch(money, cells[1])
            if match:
                table.append((cells[0], match[1] or match[2]))
            elif table_currency and re.fullmatch(number, cells[1]):
                table.append((cells[0], cells[1]))
            else:
                return []
    if ranked and table:
        return []
    if ranked and [rank for rank, _, _ in ranked] != list(range(1, len(ranked) + 1)):
        return []
    pairs = [(name, amount) for _, name, amount in ranked] or table
    if re.search(r"\bfive\b", text, re.I) and len(pairs) != 5:
        return []
    if not 2 <= len(pairs) <= 20 or len({name for name, _ in pairs}) != len(pairs):
        return []
    rows = []
    for name, amount in pairs:
        value = float(Decimal(amount.replace(",", "")))
        if not 0 <= value < float("inf"):
            return []
        rows.append({"Category": name, "Item value (BRL)": value})
    return rows


CATEGORY_LABELS = {
    "beleza_saude": "Beauty & Health", "relogios_presentes": "Watches & Gifts",
    "cama_mesa_banho": "Bed, Bath & Table", "esporte_lazer": "Sports & Leisure",
    "informatica_acessorios": "Computers & Accessories",
}


def chart_rows(result: AskResponse) -> list[dict]:
    return [{**row, "Display category": CATEGORY_LABELS.get(row["Category"], row["Category"])}
            for row in presentation_categories(result)]


def choose_visualization(result: AskResponse) -> str:
    """Choose from validated fields; unsupported prose/time series remain textual."""
    names = tool_names(result)
    if result.workflow or result.action_proposal:
        return "workflow"
    if {"get_order_financial_summary", "get_support_context"}.issubset(names):
        return "financial_support"
    if result.knowledge_sources:
        return "knowledge"
    if "run_analytical_query" in names:
        return "ranking" if presentation_categories(result) else "text"
    if financial_lines(result):
        return "financial"
    if result.external_sources:
        return "support"
    if canceled_count(result) is not None:
        return "count"
    return "text"
