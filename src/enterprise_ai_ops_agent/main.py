"""Minimal HTTP service entry point."""

from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.api.assistant import router
from enterprise_ai_ops_agent.api.workflows import router as workflow_router
from enterprise_ai_ops_agent.api.readiness import router as readiness_router
from enterprise_ai_ops_agent.api.telemetry import RequestTelemetry
from enterprise_ai_ops_agent.observability import configure_logging


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    configure_logging(configuration.log_level)
    application = FastAPI(title=configuration.app_name)
    application.state.settings = configuration
    application.include_router(router)
    application.include_router(workflow_router)
    application.include_router(readiness_router)
    application.add_middleware(RequestTelemetry)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        # The framework default includes caller input. Preserve the documented
        # validation-error shape/status without reflecting arbitrary payloads.
        return JSONResponse(status_code=422, content={"detail":[{
            "loc":["request"], "msg":"Invalid request.", "type":"request_validation_error",
        }]})

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    return application


app = create_app()
