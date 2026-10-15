"""Product identity and conservative visual selection, independent of live services."""

from pathlib import Path

import pytest

from demo.api_client import AskResponse, APIClient
from demo.presentation import presentation_categories, chart_rows, choose_visualization


def ranking(text):
    return AskResponse(answer=text, tool_calls=[{"tool_name": "run_analytical_query"}])


@pytest.mark.parametrize("row", ["{i}. category_{i} - BRL {i},987.65", "{i}. **category_{i}**: {i},987.65 BRL", "{i}. category_{i} with a total item price of {i},987.65 BRL"])
def test_numbered_formats_copy_the_current_response_values(row):
    result = ranking("\n".join(row.format(i=i) for i in range(1, 6)))
    data = presentation_categories(result)
    assert len(data) == 5
    assert data[0]["Item value (BRL)"] == 1987.65
    assert data[-1]["Item value (BRL)"] == 5987.65
    assert choose_visualization(result) == "ranking"


def test_markdown_table_uses_explicit_currency_header():
    result = ranking("| Category | Total item price (BRL) |\n|---|---:|\n| beleza_saude | 12,345.67 |\n| esporte_lazer | 8,765.43 |")
    rows = chart_rows(result)
    assert rows[0] == {"Category": "beleza_saude", "Display category": "Beauty & Health", "Item value (BRL)": 12345.67}
    assert rows[1]["Item value (BRL)"] == 8765.43
    assert result.answer.endswith("8,765.43 |")


@pytest.mark.parametrize("text", [
    "1. category_one - BRL 10.00\n2. category_two - BRL 1,,000.00",
    "1. category_one - BRL 10.00\n2. category_one - BRL 20.00",
    "1. category_one - BRL 10.00\n3. category_two - BRL 20.00",
    "| Category | Amount |\n|---|---|\n| category_one | 10.00 |\n| category_two | 20.00 |",
    "The five categories are:\n1. category_one - BRL 10.00\n2. category_two - BRL 20.00",
    "1. category_one - BRL 10.00\n2. category_two - BRL 20.00\n| Category | Value (BRL) |\n| category_three | 30.00 |",
])
def test_ambiguous_or_malformed_results_fall_back_to_original_text(text):
    result = ranking(text)
    assert choose_visualization(result) == "text"
    assert presentation_categories(result) == []
    assert result.answer == text


def test_tool_permission_and_unknown_time_series_do_not_become_charts():
    assert choose_visualization(AskResponse(answer="2020-01: 10, 2020-02: 20", tool_calls=[])) == "text"
    assert presentation_categories(AskResponse(answer="1. category_one - BRL 10.00\n2. category_two - BRL 20.00", tool_calls=[])) == []


def test_only_visible_product_identity_changes():
    source = Path("demo/app.py").read_text(encoding="utf-8-sig")
    assert 'st.title("CommerceOps Hub")' in source
    assert 'page_title="CommerceOps Hub"' in source
    assert "COMMERCE OPERATIONS" in source
    assert "Operations intelligence and controlled workflows for e-commerce teams" in source
    assert APIClient().base_url == "http://127.0.0.1:8000"
    assert '"run_analytical_query"' in Path("demo/presentation.py").read_text()
    assert Path("src/enterprise_ai_ops_agent/main.py").is_file()
