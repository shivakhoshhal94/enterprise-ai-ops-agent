"""HTTP contract and display-only regression tests; no live dependencies."""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from demo.api_client import APIClient, AskResponse, DemoAPIError, Workflow, error_message, safe_api_url
from demo.presentation import canceled_count, category_values, financial_lines, source_cards, workflow_card


def answer(**changes):
    return AskResponse.model_validate({"answer": "A returned answer", "tool_calls": [], **changes})


def session(monkeypatch, payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    client = MagicMock()
    client.__enter__.return_value = client
    client.request.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: client)
    return client


def test_client_calls_only_http_ask(monkeypatch):
    client = session(monkeypatch, {"answer": "Actual response", "tool_calls": []})
    result = APIClient().ask("A question")
    assert result.answer == "Actual response"
    client.request.assert_called_once_with("POST", "http://127.0.0.1:8000/ask", json={"question": "A question"}, timeout=(3, 240), allow_redirects=False)
    assert client.trust_env is False


@pytest.mark.parametrize("status,ready", [(200, True), (503, False)])
def test_readiness_parsing_even_when_unready(monkeypatch, status, ready):
    client = session(monkeypatch, {"ready": ready, "checks": {"database": "ready" if ready else "unavailable"}}, status)
    assert APIClient().readiness().ready is ready
    assert client.request.call_args.args[0:2] == ("GET", "http://127.0.0.1:8000/ready")
    assert client.request.call_args.kwargs["timeout"] == (3, 15)


@pytest.mark.parametrize("status", [401, 403, 422, 503, 500, 302])
def test_error_status_never_echoes_response(monkeypatch, status):
    session(monkeypatch, {"detail": "secret-password-from-provider"}, status)
    with pytest.raises(DemoAPIError) as error:
        APIClient().ask("Question")
    assert str(error.value) == error_message(status)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("failure", [requests.ConnectionError("secret URL"), requests.Timeout("secret URL")])
def test_network_failures_are_safe_and_not_retried(monkeypatch, failure):
    client = session(monkeypatch, {})
    client.request.side_effect = failure
    with pytest.raises(DemoAPIError) as error:
        APIClient().ask("Question")
    assert "secret" not in str(error.value)
    assert client.request.call_count == 1


@pytest.mark.parametrize("payload", [{"answer": 12}, {"tool_calls": []}, {"answer": "ok", "tool_calls": "secret"}])
def test_bad_answer_contract_is_sanitized(monkeypatch, payload):
    session(monkeypatch, payload)
    with pytest.raises(DemoAPIError, match="unexpected answer response"):
        APIClient().ask("Question")


def test_invalid_readiness_is_safe(monkeypatch):
    session(monkeypatch, {"ready": True, "checks": {"database": "secret"}})
    with pytest.raises(DemoAPIError, match="unexpected readiness"):
        APIClient().readiness()


def test_blank_question_does_not_call_http(monkeypatch):
    client = session(monkeypatch, {})
    with pytest.raises(DemoAPIError, match="enter a question"):
        APIClient().ask("   ")
    client.request.assert_not_called()


@pytest.mark.parametrize("url", ["postgresql://user:secret@host/db", "http://user:secret@host", "http://localhost/?secret=x", "http://localhost/#secret", "http://localhost:bad"])
def test_config_url_cannot_expose_credentials(url):
    with pytest.raises(DemoAPIError) as error:
        safe_api_url(url)
    assert url not in str(error.value)


def test_knowledge_and_external_sources_remain_separate():
    result = answer(knowledge_sources=[{"document": "policy", "title": "Demo policy", "section": "Entries"}], external_sources=[{"source_type": "mcp_support", "system": "synthetic_support_tickets", "tool": "get_support_context", "order_id": "order", "ticket_ids": ["DEMO-1"], "data_origin": "synthetic_demo"}])
    knowledge, external = source_cards(result)
    assert knowledge[0]["section"] == "Entries"
    assert external[0]["ticket_ids"] == ["DEMO-1"]
    assert external[0]["data_origin"] == "synthetic_demo"


@pytest.mark.parametrize("status,pending", [("awaiting_approval", True), ("completed", False), ("rejected", False), ("failed", False), ("approved", False)])
def test_workflow_messages_follow_backend_state(status, pending):
    workflow = Workflow(workflow_id="workflow", order_id="order", reason="Review", priority="high", status=status)
    assert workflow_card(workflow)["pending_without_action"] is pending


def test_canceled_metric_uses_returned_value_not_known_fixture():
    result = answer(answer="There are 42 orders that were canceled in the dataset.", tool_calls=[{"tool_name": "get_order_status_summary"}])
    assert canceled_count(result) == 42
    assert canceled_count(answer(answer=result.answer)) is None
    result.answer += " Another 99 orders were canceled."
    assert canceled_count(result) is None


def test_category_chart_only_uses_complete_returned_rows():
    text = "\n".join(f"{i}. category_{i} with a total item price of {i},234.56 BRL" for i in range(1, 6))
    result = answer(answer=text, tool_calls=[{"tool_name": "run_analytical_query"}])
    assert category_values(result)[0] == {"Category": "category_1", "Item value (BRL)": 1234.56}
    result.answer = "The five categories performed well."
    assert category_values(result) == []


def test_category_chart_supports_explicit_currency_rows_without_inventing_values():
    result = answer(answer="\n".join(f"{i}. **category_{i}** - BRL {i},123.45" for i in range(1, 6)), tool_calls=[{"tool_name": "run_analytical_query"}])
    assert len(category_values(result)) == 5
    assert category_values(result)[4]["Item value (BRL)"] == 5123.45
    result.answer = result.answer.replace("1,123.45", "1,,123.45")
    assert category_values(result) == []


def test_finance_copies_lines_does_not_compute_or_use_ticket_numbers():
    labels = ["Item price total", "Freight total", "Items plus freight", "Recorded payment total", "Recorded payment difference"]
    result = answer(answer="\n".join(f"- {label}: BRL 123.45" for label in labels) + "\nTicket text: 999.99", tool_calls=[{"tool_name": "get_order_financial_summary"}])
    assert len(financial_lines(result)) == 5
    assert all(row["Recorded value"] == "BRL 123.45" for row in financial_lines(result))
    result.answer = "Incomplete totals"
    assert financial_lines(result) == []


def test_demo_imports_no_backend_database_or_provider():
    for path in Path("demo").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        assert not any(name.split(".")[0] in {"enterprise_ai_ops_agent", "sqlalchemy", "psycopg", "openai"} for name in modules)
    source = Path("demo/api_client.py").read_text()
    assert "/approve" not in source and "/reject" not in source and "APPROVAL_API_KEY" not in source


def test_streamlit_home_and_input_do_not_auto_ask(monkeypatch):
    pytest.importorskip("streamlit")
    import streamlit
    streamlit.cache_data.clear()
    from streamlit.testing.v1 import AppTest
    client = session(monkeypatch, {"ready": True, "checks": {"database": "ready"}})
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "demo/app.py").run(timeout=10)
    assert not app.exception
    assert len(app.metric) == 6
    app.button[1].click().run()
    assert app.text_area[0].value == "How many orders were canceled?"
    assert all(call.args[0] == "GET" for call in client.request.call_args_list)


def test_streamlit_backend_unavailable_has_no_stack_trace(monkeypatch):
    pytest.importorskip("streamlit")
    import streamlit
    streamlit.cache_data.clear()
    from streamlit.testing.v1 import AppTest
    client = session(monkeypatch, {})
    client.request.side_effect = requests.ConnectionError("secret password")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "demo/app.py").run(timeout=10)
    assert not app.exception
    assert any("Backend unavailable" in message.value for message in app.warning)


def test_streamlit_pending_workflow_and_chart_render_without_actions(monkeypatch):
    pytest.importorskip("streamlit")
    import streamlit
    from streamlit.testing.v1 import AppTest
    streamlit.cache_data.clear()
    session(monkeypatch, {"ready": True, "checks": {"database": "ready"}})
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "demo/app.py").run(timeout=10)
    result = answer(
        answer="\n".join(f"{i}. category_{i} with a total item price of {i},234.56 BRL" for i in range(1, 6)),
        tool_calls=[{"tool_name": "run_analytical_query"}],
        workflow={"workflow_id": "workflow", "order_id": "order", "reason": "Review", "priority": "high", "status": "awaiting_approval"},
    )
    app.session_state.latest = ("Test response", result, "12:00:00")
    app.run(timeout=10)
    assert not app.exception
    assert any("Awaiting Human Approval" in message.value for message in app.warning)
    assert not any(button.label in {"Approve", "Reject"} for button in app.button)
    assert any("No business action has been executed yet." in message.value for message in app.markdown)
