"""Run existing offline evaluation suites without changing their expectations."""

from pathlib import Path
import subprocess
import sys

SUITES = (
    "test_ai_evaluations.py", "test_business_queries.py", "test_sql_safety_evaluations.py",
    "test_assistant_analytics_evaluations.py", "test_action_proposal_evaluations.py",
    "test_assistant_knowledge.py", "test_knowledge.py", "test_order_review_workflow.py",
    "test_workflow_api.py", "test_support_tickets_mcp.py", "test_assistant_support.py",
)


def main():
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-m", "pytest", "-W", "always", "-ra",
                           *(str(root / "tests" / name) for name in SUITES)], cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
