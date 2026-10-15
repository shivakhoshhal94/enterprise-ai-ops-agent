"""Small HTTP client and local projections of the public API contracts."""

from typing import Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field, ValidationError

DEFAULT_API_URL = "http://127.0.0.1:8000"


class DemoAPIError(RuntimeError):
    """Only application-owned messages may cross this boundary."""


class Readiness(BaseModel):
    ready: bool
    checks: dict[str, Literal["ready", "unavailable"]]


class ToolTrace(BaseModel):
    tool_name: str
    arguments: dict[str, str] = Field(default_factory=dict)


class KnowledgeSource(BaseModel):
    document: str
    title: str
    section: str


class ExternalSource(BaseModel):
    source_type: str
    system: str
    tool: str
    order_id: str
    ticket_ids: list[str]
    data_origin: str


class ReviewProposal(BaseModel):
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    requires_approval: Literal[True]


class Workflow(BaseModel):
    workflow_id: str
    order_id: str
    reason: str
    priority: Literal["low", "medium", "high"]
    status: Literal["awaiting_approval", "approved", "executing", "rejected", "completed", "failed"]
    case_id: str | None = None


class PresentationDataset(BaseModel):
    tool_name: str
    provenance: Literal["historical_database", "synthetic_support"]
    columns: list[str] = Field(max_length=32)
    rows: list[list[str | int | float | bool | None]] = Field(max_length=200)
    summary: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    truncated: bool = False


class AskResponse(BaseModel):
    answer: str
    presentation_data: list["PresentationDataset"] = Field(default_factory=list, max_length=3)
    tool_calls: list[ToolTrace]
    knowledge_sources: list[KnowledgeSource] = Field(default_factory=list)
    external_sources: list[ExternalSource] = Field(default_factory=list)
    action_proposal: ReviewProposal | None = None
    workflow: Workflow | None = None


def safe_api_url(value: str) -> str:
    """Reject credentials and non-HTTP URLs before displaying or requesting them."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError
        _ = parsed.port
        return value.rstrip("/")
    except ValueError:
        raise DemoAPIError("DEMO_API_URL must be an HTTP origin without credentials.") from None


def error_message(status: int) -> str:
    return {
        401: "Workflow access is not authorized.",
        403: "This operation is not permitted.",
        422: "The request was not valid.",
        503: "One or more backend services are unavailable.",
    }.get(status, "The backend could not complete the request. Please try again.")


class APIClient:
    def __init__(self, base_url: str = DEFAULT_API_URL):
        self.base_url = safe_api_url(base_url)

    def _request(self, method: str, path: str, *, payload=None, readiness=False):
        try:
            # No retries: repeating /ask could create another pending proposal.
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    method, self.base_url + path, json=payload,
                    timeout=(3, 15 if readiness else 240), allow_redirects=False,
                )
            if response.status_code != 200 and not (readiness and response.status_code == 503):
                raise DemoAPIError(error_message(response.status_code))
            return response.json()
        except requests.Timeout:
            raise DemoAPIError("The backend took too long. A proposal may still be processing; do not resubmit it automatically.") from None
        except requests.RequestException:
            raise DemoAPIError("Backend service is not running or cannot be reached.") from None
        except ValueError:
            raise DemoAPIError("The backend returned an unreadable response.") from None

    def readiness(self) -> Readiness:
        try:
            return Readiness.model_validate(self._request("GET", "/ready", readiness=True))
        except ValidationError:
            raise DemoAPIError("The backend returned an unexpected readiness response.") from None

    def ask(self, question: str) -> AskResponse:
        if not question.strip():
            raise DemoAPIError("Please enter a question.")
        try:
            return AskResponse.model_validate(self._request("POST", "/ask", payload={"question": question}))
        except ValidationError:
            raise DemoAPIError("The backend returned an unexpected answer response.") from None
