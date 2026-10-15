"""Presentation transformations preserve API wording, values and action state."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from demo.api_client import AskResponse
from demo.presentation import capability_labels, compact_brl, concise_answer, presentation_categories, category_values


def response(**fields):
    return AskResponse.model_validate({"answer": "Returned answer", "tool_calls": [], **fields})


@pytest.mark.parametrize("name,label", [
    ("get_order_status_summary", "Business Data"),
    ("get_customer_order_history", "Customer History"),
    ("get_order_financial_summary", "Financial Data"),
    ("run_analytical_query", "Read-only Analytics"),
    ("search_company_knowledge", "Company Knowledge / RAG"),
    ("get_support_context", "External Support / MCP"),
    ("propose_order_review_case", "Review Proposal"),
])
def test_business_label_preserves_exact_tool_trace(name, label):
    result = response(tool_calls=[{"tool_name": name}])
    assert capability_labels(result) == [label]
    assert result.tool_calls[0].tool_name == name


@pytest.mark.parametrize("value,expected", [(1258681.34, "BRL 1.26M"), (988048.97, "BRL 988.05K"), (0, "BRL 0.00"), (298.9, "BRL 298.90")])
def test_currency_display_formatting(value, expected):
    assert compact_brl(value) == expected


def test_comparison_cards_can_use_a_consistent_millions_scale():
    assert compact_brl(988048.97, millions=True) == "BRL 0.99M"


def test_currency_suffix_format_is_presentation_only_and_copies_values():
    result = response(answer="\n".join(f"{i}. category_{i} - {i},123.45 BRL" for i in range(1, 6)), tool_calls=[{"tool_name": "run_analytical_query"}])
    assert presentation_categories(result)[0]["Item value (BRL)"] == 1123.45
    assert category_values(result) == []  # Normal-mode extraction remains unchanged.


def test_presentation_category_parser_refuses_incomplete_rows():
    result = response(answer="1. category_one - 123.45 BRL", tool_calls=[{"tool_name": "run_analytical_query"}])
    assert presentation_categories(result) == []


def test_concise_answer_only_removes_appended_footer():
    wording = "Payment values are summed across entries, not multiplied by installments."
    result = response(answer=wording + "\n\nKnowledge evidence is application-owned demo documentation, not official Olist policy.\nSources follow.")
    original = result.answer
    assert concise_answer(result) == wording
    assert result.answer == original
    assert concise_answer(response(answer="No footer.\nKeep the second paragraph.")) == "No footer.\nKeep the second paragraph."


def app(monkeypatch):
    pytest.importorskip("streamlit")
    import streamlit
    from streamlit.testing.v1 import AppTest
    streamlit.cache_data.clear()
    http_response = MagicMock(status_code=200)
    http_response.json.return_value = {"ready": True, "checks": {name: "ready" for name in ["database", "analytics", "knowledge", "workflow", "openai_configuration", "approval_configuration", "mcp_configuration"]}}
    http_session = MagicMock()
    http_session.__enter__.return_value = http_session
    http_session.request.return_value = http_response
    monkeypatch.setattr(requests, "Session", lambda: http_session)
    ui = AppTest.from_file(Path(__file__).resolve().parents[1] / "demo/app.py").run(timeout=10)
    ui.toggle[0].set_value(True).run(timeout=10)
    return ui, http_session


def show_result(ui, result):
    ui.session_state.latest = ("Actual test question", result, "12:34:56")
    ui.session_state.presentation_screen = "result"
    return ui.run(timeout=10)


def test_presentation_overview_and_normal_mode_are_separate(monkeypatch):
    ui, http = app(monkeypatch)
    assert not ui.exception
    assert len(ui.metric) == 6
    assert not ui.sidebar.title
    assert any("Verified demo dataset" in caption.value for caption in ui.caption)
    assert not any("API:" in text.value for text in ui.markdown)
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)
    ui.toggle[0].set_value(False).run()
    assert not ui.exception
    assert ui.sidebar.title[0].value == "CommerceOps"
    assert len(ui.metric) == 6


def test_analytics_is_compact_and_full_answer_is_retained(monkeypatch):
    ui, http = app(monkeypatch)
    answer = "\n".join(f"{i}. category_{i} - BRL {i},123.45" for i in range(1, 6))
    ui.session_state.latest = ("Which categories have the highest item value?", response(answer=answer, tool_calls=[{"tool_name": "run_analytical_query"}]), "12:34:56")
    ui.session_state.presentation_screen = "result"
    ui.run(timeout=10)
    assert not ui.exception
    assert len(ui.metric) == 5
    assert any(h.value == "Top Product Categories by Item Value" for h in ui.subheader)
    assert ui.expander[0].label == "View full response"
    assert any(m.value == answer for m in ui.markdown)
    assert not any("Received at" in caption.value for caption in ui.caption)
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


def test_knowledge_sources_and_verbatim_answer_render(monkeypatch):
    ui, _ = app(monkeypatch)
    result = response(answer="Amounts are summed, not multiplied.", knowledge_sources=[{"document": "policy", "title": "Payment Policy", "section": "Entry semantics"}], tool_calls=[{"tool_name": "search_company_knowledge"}])
    show_result(ui, result)
    assert not ui.exception
    assert any(h.value == "Company Knowledge" for h in ui.subheader)
    assert any(m.value == result.answer for m in ui.markdown)
    assert any("Entry semantics" in caption.value for caption in ui.caption)


def test_mixed_source_values_and_ticket_count_come_from_response(monkeypatch):
    ui, _ = app(monkeypatch)
    labels = ["Item price total", "Freight total", "Items plus freight", "Recorded payment total", "Recorded payment difference"]
    result = response(answer="\n".join(f"- {label}: BRL 321.09" for label in labels), tool_calls=[{"tool_name": "get_order_financial_summary"}, {"tool_name": "get_support_context"}], external_sources=[{"source_type": "mcp_support", "system": "test_support", "tool": "get_support_context", "order_id": "order", "ticket_ids": ["DEMO-1", "DEMO-2"], "data_origin": "synthetic_demo"}])
    show_result(ui, result)
    assert not ui.exception
    assert ui.metric[0].value == "2"
    assert any("These sources are independent" in notice.value for notice in ui.info)
    assert sum(m.value == "BRL 321.09" for m in ui.markdown) == 5


@pytest.mark.parametrize("status,pending", [("awaiting_approval", True), ("completed", False), ("rejected", False)])
def test_proposal_claims_follow_actual_state_without_approval_controls(monkeypatch, status, pending):
    ui, http = app(monkeypatch)
    result = response(workflow={"workflow_id": "workflow", "order_id": "order", "reason": "Returned reason", "priority": "high", "status": status}, tool_calls=[{"tool_name": "propose_order_review_case"}])
    show_result(ui, result)
    assert not ui.exception
    assert any(h.value == "Internal Review Request" for h in ui.subheader)
    assert any(m.value == "No business action has been executed yet." for m in ui.markdown) is pending
    assert not any(button.label in {"Approve", "Reject"} for button in ui.button)
    assert all(call.args[0] == "GET" for call in http.request.call_args_list)


def test_structured_workflow_can_label_capability_without_fabricating_tool_trace(monkeypatch):
    ui, _ = app(monkeypatch)
    result = response(workflow={"workflow_id": "workflow", "order_id": "order", "reason": "Returned reason", "priority": "medium", "status": "awaiting_approval"})
    show_result(ui, result)
    assert not ui.exception
    assert any("Capabilities used: Review Proposal" in m.value for m in ui.markdown)
    assert result.tool_calls == []
