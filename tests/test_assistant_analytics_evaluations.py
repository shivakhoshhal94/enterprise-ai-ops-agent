"""Scripted routing plus real SQL-result fixtures; not live model accuracy."""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlglot
from sqlglot import exp
from sqlalchemy import create_engine

from enterprise_ai_ops_agent.ai import assistant, tool_executor
from enterprise_ai_ops_agent.analytics.models import AnalyticalResult
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db.models import Customer, Order, OrderItem, OrderPayment, Product, Seller

CASES = json.loads((Path(__file__).resolve().parents[1] / "evals" / "assistant_analytics.json").read_text(encoding="utf-8"))


@pytest.fixture
def fixture_database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(Customer.__table__.insert(), [
            {"customer_id": f"c{index}", "customer_unique_id": f"u{index}", "customer_zip_code_prefix": "00001", "customer_city": "city", "customer_state": state}
            for index, state in enumerate(("SP", "RJ"), 1)
        ])
        connection.execute(Seller.__table__.insert(), {"seller_id": "s", "seller_zip_code_prefix": "00001", "seller_city": "city", "seller_state": "SP"})
        connection.execute(Product.__table__.insert(), [{"product_id": "p", "product_category_name": "books"}, {"product_id": "q", "product_category_name": "untranslated"}])
        connection.execute(Order.__table__.insert(), [
            {"order_id": f"o{index}", "customer_id": f"c{index}", "order_status": "delivered", "order_purchase_timestamp": datetime(2017, index, 1), "order_estimated_delivery_date": datetime(2017, 3, 1)}
            for index in (1, 2)
        ])
        connection.execute(OrderItem.__table__.insert(), [
            {"order_id": order, "order_item_id": sequence, "product_id": product, "seller_id": "s", "shipping_limit_date": datetime(2017, 3, 1), "price": Decimal(price), "freight_value": Decimal(freight)}
            for order, sequence, product, price, freight in (("o1", 1, "p", "10", "2"), ("o1", 2, "p", "20", "2"), ("o2", 1, "q", "50", "0"))
        ])
        connection.execute(OrderPayment.__table__.insert(), [
            {"order_id": order, "payment_sequential": sequence, "payment_type": "credit_card", "payment_installments": 3, "payment_value": Decimal(value)}
            for order, sequence, value in (("o1", 1, "12"), ("o1", 2, "23"), ("o2", 1, "50"))
        ])
    yield engine
    engine.dispose()


def evaluate_fixture_query(engine, query):
    # Test-only dialect translation: production remains strictly PostgreSQL.
    tree = sqlglot.parse_one(query.normalized_sql, read="postgres")
    for table in tree.find_all(exp.Table):
        table.set("db", None)
    # SQLGlot leaves EXTRACT unchanged for SQLite; adapt only these fixture date parts.
    for extract in list(tree.find_all(exp.Extract)):
        unit = {"YEAR": "%Y", "MONTH": "%m"}[extract.this.name.upper()]
        extract.replace(sqlglot.parse_one(
            f"CAST(STRFTIME('{unit}', {extract.expression.sql(dialect='sqlite')}) AS INTEGER)", read="sqlite",
        ))
    with engine.connect() as connection:
        result = connection.exec_driver_sql(tree.sql(dialect="sqlite"))
        columns = list(result.keys())
        rows = [list(row) for row in result]
    for row in rows:
        for index, column in enumerate(columns):
            if column in ("item_value", "difference"):
                row[index] = Decimal(str(row[index])).quantize(Decimal("0.01"))
    return AnalyticalResult(columns=columns, rows=rows, returned_row_count=len(rows), truncated=False)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_assistant_analytical_scenario(case, fixture_database, monkeypatch, response_factory, fake_client):
    execute = MagicMock(side_effect=lambda settings, query: evaluate_fixture_query(fixture_database, query))
    monkeypatch.setattr(tool_executor, "execute_configured_analytical_query", execute)
    call = response_factory(calls=[("run_analytical_query", json.dumps({"sql": case["sql"]}))])
    responses = [call, call] if case.get("reject") else [call, response_factory(answer="Scripted answer grounded in tool result.")]
    client = fake_client(responses)
    admin = MagicMock()
    configuration = Settings(_env_file=None, analytics_database_url="postgresql+psycopg://enterprise_ai_ops_reader:fake@localhost/project")
    if case.get("reject"):
        with pytest.raises(assistant.AssistantToolError):
            assistant.ask_business_assistant(case["question"], client=client, engine=admin, model="fake", settings=configuration)
        execute.assert_not_called()
    else:
        result = assistant.ask_business_assistant(case["question"], client=client, engine=admin, model="fake", settings=configuration)
        assert result.tool_calls[0].tool_name == "run_analytical_query"
        output = json.loads(client.responses.create.call_args_list[1].kwargs["input"][-1]["output"])
        assert output["rows"] == case["rows"]
        assert output["truncated"] is False
    admin.connect.assert_not_called()


def test_raw_monetary_join_fails_semantic_fixture(fixture_database):
    correct = next(case for case in CASES if case["id"] == "monetary_grain")
    unsafe_grain = "SELECT i.order_id,SUM(p.payment_value)-SUM(i.price+i.freight_value) AS difference FROM order_items i JOIN order_payments p ON p.order_id=i.order_id GROUP BY i.order_id ORDER BY i.order_id"
    # Safety validation intentionally accepts this read-only query; semantic evaluation rejects its result.
    actual = evaluate_fixture_query(fixture_database, validate_analytical_sql(unsafe_grain)).model_dump(mode="json")["rows"]
    assert actual == [["o1", "2.00"], ["o2", "0.00"]]
    assert actual != correct["rows"]
