"""Developer-only MCP verification; no LLM, database configuration or HTTP endpoint."""

import argparse
from functools import partial
import sys

import anyio
from enterprise_ai_ops_agent.integrations.support_tickets.client import get_support_context, SupportIntegrationError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = anyio.run(partial(get_support_context, arguments.order_id))
    except SupportIntegrationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
