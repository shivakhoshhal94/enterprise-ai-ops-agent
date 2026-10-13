"""Explicit fixed-server stdio MCP call; no direct server implementation invocation."""

import math
import os
from pathlib import Path
import sys

import anyio
from mcp import Client, StdioServerParameters, stdio_client
from pydantic import ValidationError

from enterprise_ai_ops_agent.integrations.support_tickets.contracts import SupportContext, SupportRequest

SERVER_DIRECTORY = Path(__file__).resolve().parents[4] / "mcp_servers" / "support_tickets"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 15.0


class SupportIntegrationError(RuntimeError):
    """Safe public failure without protocol, command or environment details."""
    code = "unavailable"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class InvalidSupportRequest(SupportIntegrationError):
    code = "invalid_request"


class SupportIntegrationTimeout(SupportIntegrationError):
    code = "timeout"


def server_parameters() -> StdioServerParameters:
    # SDK merges only its OS-variable allowlist; no application Settings or os.environ copy.
    return StdioServerParameters(command=sys.executable, args=["-I", str(SERVER_DIRECTORY / "server.py")],
                                 cwd=SERVER_DIRECTORY, env={"PYTHONUNBUFFERED":"1", "PYTHONIOENCODING":"utf-8"})


async def get_support_context(order_id: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> SupportContext:
    try:
        request = SupportRequest(order_id=order_id)
    except ValueError:
        raise InvalidSupportRequest("Provide a 32-character lowercase hexadecimal order_id.") from None
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int,float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise InvalidSupportRequest("Support timeout must be positive and at most 15 seconds.")
    stage = "startup"
    try:
        # Discard child stderr: it is not a safe application error channel.
        with open(os.devnull, "w", encoding="utf-8") as errors:
            with anyio.fail_after(timeout_seconds):
                async with Client(stdio_client(server_parameters(), errlog=errors),
                                  read_timeout_seconds=timeout_seconds) as session:
                    stage = "service"
                    inventory = await session.list_tools()
                    if (inventory.next_cursor is not None or len(inventory.tools)!=1
                            or inventory.tools[0].name!="get_support_context"
                            or inventory.tools[0].input_schema!=SupportRequest.model_json_schema()
                            or inventory.tools[0].output_schema!=SupportContext.model_json_schema()
                            or inventory.tools[0].annotations is None
                            or inventory.tools[0].annotations.read_only_hint is not True):
                        raise SupportIntegrationError("Unexpected support capability contract.", code="contract")
                    result = await session.call_tool("get_support_context", request.model_dump(),
                                                     read_timeout_seconds=timeout_seconds)
                    if result.is_error:
                        raise SupportIntegrationError("Support service could not complete the request.", code="tool_failure")
                    if result.structured_content is None:
                        raise SupportIntegrationError("Support service returned an invalid response.", code="invalid_response")
                    context = SupportContext.model_validate(result.structured_content)
                    if context.order_id != request.order_id:
                        raise SupportIntegrationError("Support response does not match the request.", code="invalid_response")
                    return context
    except SupportIntegrationError:
        raise
    except ExceptionGroup as error:
        # Official session/transport task groups can wrap the boundary's own
        # validation failure. Classify leaves without exposing protocol text.
        if error.subgroup(TimeoutError) is not None:
            raise SupportIntegrationTimeout("Support request timed out.") from None
        if error.subgroup(ValidationError) is not None:
            raise SupportIntegrationError("Support service returned an invalid response.", code="invalid_response") from None
        known = error.subgroup(SupportIntegrationError)
        if known is not None:
            while isinstance(known, ExceptionGroup):
                known = known.exceptions[0]
            raise SupportIntegrationError("Support service is unavailable or returned an invalid response.", code=known.code) from None
        raise SupportIntegrationError("Support service is unavailable or returned an invalid response.") from None
    except TimeoutError:
        raise SupportIntegrationTimeout("Support request timed out.") from None
    except ValidationError:
        raise SupportIntegrationError("Support service returned an invalid response.", code="invalid_response") from None
    except OSError:
        raise SupportIntegrationError("Support service is unavailable or returned an invalid response.",
                                      code="startup" if stage == "startup" else "unavailable") from None
    except Exception:
        raise SupportIntegrationError("Support service is unavailable or returned an invalid response.") from None
