"""Explicit bounded read-only diagnostics; no provider calls, processes, or setup."""

import os
from pathlib import Path
import sys
from typing import Annotated, Literal
from collections.abc import Callable
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine, create_analytics_engine
from enterprise_ai_ops_agent.knowledge.session import create_knowledge_engine
from enterprise_ai_ops_agent.integrations.support_tickets.client import SERVER_DIRECTORY
from enterprise_ai_ops_agent.observability import emit, error_category, timed

router = APIRouter(tags=["operations"])
Status = Literal["ready", "unavailable"]


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: Status
    analytics: Status
    knowledge: Status
    workflow: Status
    openai_configuration: Status
    approval_configuration: Status
    mcp_configuration: Status


class ReadinessResponse(BaseModel):
    ready: bool
    checks: ReadinessChecks


def database_check(settings, factory, component, statements):
    engine = None
    status = "unavailable"
    try:
        with timed(component):
            engine = factory(settings, connect_timeout=2)
            with engine.connect().execution_options(postgresql_readonly=True) as connection:
                connection.execute(text("SELECT set_config('statement_timeout','1000',true)"))
                for statement in statements:
                    result = connection.execute(text(statement))
                    if component == "knowledge":
                        profiles = result.mappings().all()
                        if not profiles or any(row["embedding_model"] != settings.openai_embedding_model
                            or row["embedding_dimensions"] != settings.openai_embedding_dimensions for row in profiles):
                            raise ValueError("Knowledge embedding configuration is not ready.")
        status = "ready"
    except Exception:
        pass
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as error:
                status = "unavailable"
                emit("dependency_failed", component, outcome="failed", error_category=error_category(error, component))
    return status


def configuration_present(value):
    return value is not None and bool(value.get_secret_value().strip())


def check_readiness(settings: Settings) -> ReadinessResponse:
    # Trusted static statements only. LIMIT 0 validates schema/access without state reads.
    checks = ReadinessChecks(
        database=database_check(settings, create_db_engine, "database", ["SELECT 1"]),
        analytics=database_check(settings, create_analytics_engine, "analytics", ["SELECT 1"] + [
            f"SELECT 1 FROM public.{name} LIMIT 0" for name in
            ("customers","category_translations","sellers","products","orders","order_items","order_payments")]),
        knowledge=database_check(settings, create_knowledge_engine, "knowledge",
                                 ["SELECT DISTINCT embedding_model, embedding_dimensions FROM knowledge.document_chunks LIMIT 2"]),
        workflow=database_check(settings, create_db_engine, "workflow", [
            f"SELECT 1 FROM workflow_checkpoints.{name} LIMIT 0" for name in
            ("checkpoints","checkpoint_blobs","checkpoint_writes","checkpoint_migrations")]),
        openai_configuration="ready" if configuration_present(settings.openai_api_key) and settings.openai_model.strip()
            and settings.openai_embedding_model.strip() else "unavailable",
        approval_configuration="ready" if configuration_present(settings.approval_api_key) else "unavailable",
        mcp_configuration="ready" if (Path(sys.executable).is_file() and os.access(sys.executable, os.X_OK)
            and (SERVER_DIRECTORY/"server.py").is_file()
            and (SERVER_DIRECTORY/"data"/"support_tickets.json").is_file()) else "unavailable",
    )
    for name in ("openai_configuration", "approval_configuration", "mcp_configuration"):
        available = getattr(checks, name) == "ready"
        emit("configuration_check_completed" if available else "configuration_check_failed", name,
             outcome="completed" if available else "failed",
             **({"error_category":"configuration_error"} if not available else {}))
    return ReadinessResponse(ready=all(value=="ready" for value in checks.model_dump().values()), checks=checks)


def get_readiness(request: Request):
    return lambda: check_readiness(request.app.state.settings)


@router.get("/ready", response_model=ReadinessResponse, responses={503:{"model":ReadinessResponse}},
            summary="Read-only dependency readiness; provider and MCP configuration only")
def ready(response: Response, check: Annotated[Callable[[], ReadinessResponse], Depends(get_readiness)]) -> ReadinessResponse:
    result = check()
    response.status_code = 200 if result.ready else 503
    return result
