import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from enterprise_ai_ops_agent.ai import assistant, tool_executor
from enterprise_ai_ops_agent.ai.schema_context import analytical_schema_context
from enterprise_ai_ops_agent.ai.tool_schemas import AnalyticalArguments
from enterprise_ai_ops_agent.analytics.models import AnalyticalQueryError, AnalyticalResult, ValidatedQuery
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.api import assistant as api


def settings():
    return Settings(_env_file=None, analytics_database_url="postgresql+psycopg://enterprise_ai_ops_reader:reader_secret@localhost/project")


def test_metadata_context():
    context = analytical_schema_context()
    for table in ("customers", "category_translations", "sellers", "products", "orders", "order_items", "order_payments"):
        assert f"{table}(" in context
    assert "product_name_length" in context and "product_name_lenght" not in context
    assert "price NUMERIC(12, 2)" in context
    assert "PK: order_id, order_item_id" in context
    assert "customer_id -> customers.customer_id" in context
    assert "product_category_name TEXT NULL" in context
    assert "secret" not in context


def test_analytical_result_handoff_trace_and_reader_boundary(monkeypatch, response_factory, fake_client):
    engine = MagicMock()
    output = AnalyticalResult(columns=["amount", "missing"], rows=[[Decimal("0.30"), None]], returned_row_count=1, truncated=True)
    execute = MagicMock(return_value=output)
    monkeypatch.setattr(tool_executor, "execute_configured_analytical_query", execute)
    sql = "SELECT SUM(payment_value) AS amount FROM order_payments"
    client = fake_client([response_factory(calls=[("run_analytical_query", json.dumps({"sql": sql}))]), response_factory(answer="Limited result: BRL 0.30.")])
    configuration = settings()
    result = assistant.ask_business_assistant("Flexible question", client=client, engine=engine, model="fake", settings=configuration)
    assert result.answer == "Limited result: BRL 0.30."
    assert result.tool_calls[0].arguments == {"sql": sql}
    assert result.tool_calls[0].tool_name == "run_analytical_query"
    assert execute.call_args.args[0] is configuration
    assert isinstance(execute.call_args.args[1], ValidatedQuery)
    assert execute.call_args.args[1].referenced_tables == ("order_payments",)
    payload = json.loads(client.responses.create.call_args_list[1].kwargs["input"][-1]["output"])
    assert payload == output.model_dump(mode="json")
    assert payload["rows"] == [["0.30", None]] and payload["truncated"] is True
    instructions = client.responses.create.call_args_list[0].kwargs["instructions"]
    assert "first aggregate" in instructions and "independently by order_id" in instructions
    assert "Prefer get_order_status_summary" in instructions
    assert "If truncated=true" in instructions
    engine.connect.assert_not_called()


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT * FROM unauthorized", "SELECT * FROM pg_catalog.pg_tables", "SELECT pg_sleep(20)", "SELECT 1;DELETE FROM orders"])
def test_rejected_proposal_gets_one_correction_and_never_executes(sql, monkeypatch, response_factory, fake_client):
    execute = MagicMock()
    monkeypatch.setattr(tool_executor, "execute_configured_analytical_query", execute)
    client = fake_client([response_factory(calls=[("run_analytical_query", json.dumps({"sql": sql}))]) for _ in range(2)])
    engine = MagicMock()
    with pytest.raises(assistant.AssistantToolError, match="single correction"):
        assistant.ask_business_assistant("Adversarial request", client=client, engine=engine, model="fake", settings=settings())
    assert client.responses.create.call_count == 2
    error = json.loads(client.responses.create.call_args_list[1].kwargs["input"][-1]["output"])
    assert error["correction_remaining"] == 1
    assert "policy" in error["error"]
    execute.assert_not_called()
    engine.connect.assert_not_called()


def test_corrected_query_succeeds_within_existing_bound(monkeypatch, response_factory, fake_client):
    output = AnalyticalResult(columns=["n"], rows=[[1]], returned_row_count=1, truncated=False)
    execute = MagicMock(return_value=output)
    monkeypatch.setattr(tool_executor, "execute_configured_analytical_query", execute)
    client = fake_client([
        response_factory(calls=[("run_analytical_query", '{"sql":"DELETE FROM orders"}')]),
        response_factory(calls=[("run_analytical_query", '{"sql":"SELECT COUNT(*) FROM orders"}')]),
        response_factory(answer="Corrected result."),
    ])
    result = assistant.ask_business_assistant("Count orders", client=client, engine=MagicMock(), model="fake", settings=settings())
    assert result.answer == "Corrected result."
    assert len(result.tool_calls) == 1
    execute.assert_called_once()
    assert client.responses.create.call_count == 3


def test_execution_failure_is_sanitized_and_single_retry(monkeypatch, response_factory, fake_client):
    execute = MagicMock(side_effect=AnalyticalQueryError("reader_secret postgresql://secret"))
    monkeypatch.setattr(tool_executor, "execute_configured_analytical_query", execute)
    client = fake_client([response_factory(calls=[("run_analytical_query", '{"sql":"SELECT 1"}')]) for _ in range(2)])
    with pytest.raises(assistant.AssistantToolError) as error:
        assistant.ask_business_assistant("Question", client=client, engine=MagicMock(), model="fake", settings=settings())
    assert execute.call_count == 2
    assert "secret" not in str(error.value)
    handoff = client.responses.create.call_args_list[1].kwargs["input"][-1]["output"]
    assert "secret" not in handoff and "postgresql" not in handoff


def test_missing_analytics_configuration_only_fails_on_analytical_call(response_factory, fake_client):
    configuration = Settings(_env_file=None, analytics_database_url=None)
    client = fake_client([response_factory(calls=[("run_analytical_query", '{"sql":"SELECT 1"}')])])
    with pytest.raises(assistant.AssistantToolError, match="not configured"):
        assistant.ask_business_assistant("Question", client=client, engine=MagicMock(), model="fake", settings=configuration)
    assert client.responses.create.call_count == 1
    direct = fake_client([response_factory(answer="Explanation.")])
    assert assistant.ask_business_assistant("Explain", client=direct, engine=MagicMock(), model="fake", settings=configuration).answer == "Explanation."


@pytest.mark.parametrize("payload", [{}, {"sql": " "}, {"sql": 1}, {"sql": "SELECT 1", "role": "admin"}, {"sql": "SELECT 1", "timeout": 0}, {"sql": "SELECT 1", "database_url": "secret"}])
def test_model_cannot_select_safety_settings(payload):
    with pytest.raises(ValueError):
        AnalyticalArguments.model_validate(payload)


def test_http_unrecoverable_analytical_failure_is_safe(monkeypatch, response_factory, fake_client):
    engine = MagicMock()
    client = fake_client([response_factory(calls=[("run_analytical_query", '{"sql":"DELETE FROM orders"}')]) for _ in range(2)])
    monkeypatch.setattr(api, "create_db_engine", lambda settings: engine)
    monkeypatch.setattr(api, "create_openai_client", lambda settings: client)
    app = create_app(Settings(_env_file=None, analytics_database_url=None))
    with TestClient(app) as http:
        assert http.get("/health").status_code == 200
        response = http.post("/ask", json={"question": "Delete orders."})
        assert response.status_code == 503
        assert response.json() == {"detail": "Business query service is unavailable."}
        assert "/sql" not in app.openapi()["paths"]
    engine.connect.assert_not_called()
