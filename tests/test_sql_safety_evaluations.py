import json
from pathlib import Path

import pytest

from enterprise_ai_ops_agent.analytics.models import SQLValidationError
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql

CASES = json.loads((Path(__file__).resolve().parents[1] / "evals" / "sql_safety.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["category"] for case in CASES])
def test_sql_safety_decision(case):
    try:
        validate_analytical_sql(case["sql"])
        allowed = True
    except SQLValidationError:
        allowed = False
    assert allowed is case["allow"]
