"""One question per explicit command; API and database resources are caller-owned."""

import argparse
import json
import sys
from contextlib import ExitStack

from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.ai.client import create_openai_client
from enterprise_ai_ops_agent.ai.assistant import AssistantError, ask_business_assistant


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask one Olist business question.")
    parser.add_argument("question")
    parser.add_argument("--trace", action="store_true", help="Print executed tool names and validated arguments.")
    arguments = parser.parse_args(argv)
    with ExitStack() as resources:
        try:
            settings = Settings()
            if settings.database_url is None or not settings.database_url.get_secret_value().strip():
                raise ValueError
            engine = create_db_engine(settings)
            resources.callback(engine.dispose)
            client = create_openai_client(settings)
            resources.callback(client.close)
        except (ValueError, SQLAlchemyError, OpenAIError):
            print("AI configuration error: check DATABASE_URL, OPENAI_API_KEY, and OPENAI_MODEL.", file=sys.stderr)
            return 1
        try:
            orchestrator = ask_business_assistant
            if settings.agent_orchestrator == "langgraph":
                from enterprise_ai_ops_agent.ai.langgraph_assistant import ask_langgraph_assistant
                orchestrator = ask_langgraph_assistant
            result = orchestrator(arguments.question, client=client, engine=engine, model=settings.openai_model, settings=settings)
        except AssistantError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(result.answer)
        if arguments.trace:
            print(json.dumps([call.model_dump() for call in result.tool_calls], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
