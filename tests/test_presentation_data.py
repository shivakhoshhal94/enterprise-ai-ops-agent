import json

import pytest

from enterprise_ai_ops_agent.ai.presentation_data import project_tool_result
from enterprise_ai_ops_agent.ai.assistant import AssistantResult
from demo.api_client import PresentationDataset
from demo.visualizations import visual_kind, series_rows


def dataset(columns, rows):
    return PresentationDataset(tool_name="run_analytical_query", provenance="historical_database", columns=columns, rows=rows)


@pytest.mark.parametrize("columns,rows,expected", [
    (["count"], [[42]], "scalar"),
    (["category", "count"], [["a", 9], ["b", 3]], "bar"),
    (["month", "orders"], [["2017-01", 9], ["2017-02", 3]], "line"),
    (["date", "orders"], [["2017-02-30", 9], ["2017-03-01", 3]], "table"),
    (["category", "count", "amount"], [["a", 9, 3]], "table"),
    (["date", "count"], [["2017-01", 9], ["2017-01", 3]], "table"),
    (["a", "b"], [["a"]], "text"),
    (["purchase_year", "purchase_month", "order_count"], [["2017", "1", 22]], "line"),
])
def test_deterministic_visual_selection(columns, rows, expected):
    assert visual_kind(dataset(columns, rows)) == expected


def test_explicit_year_month_pairs_preserve_values_and_gaps():
    data = dataset(["purchase_year", "purchase_month", "order_count"], [[2017, 3, 9], [2017, 1, 4]])
    assert [row["Source"] for row in series_rows(data)] == ["2017-01", "2017-03"]
    assert [row["Value"] for row in series_rows(data)] == [4, 9]


def test_measure_column_is_not_assumed_to_be_last():
    data = dataset(["order_count", "purchase_year", "purchase_month"], [[44, 2017, 2]])
    assert series_rows(data)[0]["Measure"] == "order_count"
    assert series_rows(data)[0]["Value"] == 44


def test_empty_projection_is_additive_and_omitted_for_legacy_stub_responses():
    assert "presentation_data" not in AssistantResult(answer="ok", tool_calls=[]).model_dump()


def test_analytics_rows_are_copied_not_generated_and_sql_is_absent():
    payload = {"columns": ["category", "amount"], "rows": [["other_category", "123.45"]], "returned_row_count": 1, "truncated": False}
    data = project_tool_result("run_analytical_query", json.dumps(payload))
    assert data.rows == payload["rows"]
    assert "sql" not in data.model_dump_json()
    assert data.provenance == "historical_database"


def test_oversize_and_unsupported_projection_fail_closed():
    payload = {"columns": ["x"], "rows": [[i] for i in range(201)], "returned_row_count": 201, "truncated": False}
    assert project_tool_result("run_analytical_query", json.dumps(payload)) is None
    assert project_tool_result("search_company_knowledge", '{"provider_secret":"hidden"}') is None
    assert project_tool_result("run_analytical_query", '{"columns":[],"rows":[["secret"]],"returned_row_count":1,"truncated":false}') is None
    assert project_tool_result("run_analytical_query", '{"columns":["x","x"],"rows":[[1,2]],"returned_row_count":1,"truncated":false}') is None


def test_money_projection_preserves_exact_decimal_strings():
    payload = {"order_id": "order", "item_count": 1, "seller_count": 1, "payment_row_count": 1,
               "items_price_total": "1.01", "freight_total": "0.02", "items_and_freight_total": "1.03", "payment_total": "1.03", "difference": "0.00"}
    data = project_tool_result("get_order_financial_summary", json.dumps(payload))
    assert data.summary["payment_total"] == "1.03"
    assert data.summary["difference"] == "0.00"


def test_assistant_copies_executed_result_without_extra_provider_calls(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from enterprise_ai_ops_agent.ai import assistant
    from enterprise_ai_ops_agent.ai.tool_executor import ToolExecution, ToolCallTrace
    payload = '[{"order_status":"canceled","order_count":41}]'
    executed = ToolExecution(trace=ToolCallTrace(tool_name="get_order_status_summary", arguments={}), output_json=payload)
    monkeypatch.setattr(assistant, "execute_tool", lambda *args, **kwargs: executed)
    client = MagicMock()
    client.responses.create.side_effect = [
        SimpleNamespace(status="completed", output=[SimpleNamespace(type="function_call",name="get_order_status_summary",arguments="{}",call_id="call")],output_text=""),
        SimpleNamespace(status="completed",output=[],output_text="Unchanged final answer"),
    ]
    result = assistant.ask_business_assistant("Count canceled orders",client=client,engine=MagicMock(),model="test")
    assert result.answer == "Unchanged final answer"
    assert result.presentation_data[0].rows == [["canceled",41]]
    assert client.responses.create.call_count == 2
