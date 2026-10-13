"""Developer-only durable review workflow commands; no HTTP or LLM integration."""

import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseIntent
from enterprise_ai_ops_agent.operations.proposals import ProposalValidationError, propose_order_review_case
from enterprise_ai_ops_agent.workflows.contracts import ApprovalDecision, WorkflowError
from enterprise_ai_ops_agent.workflows.persistence import initialize_checkpointer, postgres_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    start = commands.add_parser("start")
    for field in ("order-id", "reason", "priority"):
        start.add_argument(f"--{field}", required=True)
    for name in ("approve", "reject", "status", "retry"):
        command = commands.add_parser(name)
        command.add_argument("--workflow-id", required=True)
        if name in ("approve", "reject"):
            command.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if args.command == "setup":
            initialize_checkpointer(settings)
            print("PostgreSQL workflow checkpointer initialized (framework-owned tables).")
            return 0
        intent = None
        decision = None
        if args.command == "start":
            intent = OrderReviewCaseIntent(order_id=args.order_id, reason=args.reason, priority=args.priority)
        elif args.command in ("approve", "reject"):
            decision = ApprovalDecision(decision="approve" if args.command == "approve" else "reject", actor=args.actor)
        with postgres_workflow(settings) as workflow:
            if intent is not None:
                # Reuse the existing proposal path and its read-only existence check.
                engine = create_db_engine(settings)
                try:
                    with engine.connect().execution_options(postgresql_readonly=True) as connection:
                        proposal = propose_order_review_case(connection, intent)
                finally:
                    engine.dispose()
                result = workflow.start(proposal)
            elif decision is not None:
                result = workflow.resume(args.workflow_id, decision)
            elif args.command == "retry":
                result = workflow.retry(args.workflow_id)
            else:
                result = workflow.get_state(args.workflow_id)
        print(result.model_dump_json())
        return 1 if result.status == "failed" else 0
    except WorkflowError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, ProposalValidationError, SQLAlchemyError):
        print("Workflow configuration or input error: check required values and DATABASE_URL.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
