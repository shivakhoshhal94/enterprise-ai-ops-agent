"""Developer-only action entry point; never called by the assistant."""

import argparse
import sys

from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.operations.contracts import CreateOrderReviewCaseRequest
from enterprise_ai_ops_agent.operations.service import ActionError, create_order_review_case


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-order-review-case")
    for field in ("order-id", "reason", "priority", "idempotency-key"):
        create.add_argument(f"--{field}", required=True)
    args = parser.parse_args(argv)
    try:
        request = CreateOrderReviewCaseRequest(
            order_id=args.order_id, reason=args.reason,
            priority=args.priority, idempotency_key=args.idempotency_key,
        )
    except ValueError:
        print("Invalid review-case input: check required values, lengths and priority.", file=sys.stderr)
        return 1
    try:
        engine = create_db_engine(Settings())
    except (ValueError, SQLAlchemyError):
        print("Database configuration error: check DATABASE_URL and settings.", file=sys.stderr)
        return 1
    try:
        result = create_order_review_case(engine, request, actor="developer_cli")
    except ActionError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
