"""Pure ASGI request correlation: context also flows into FastAPI worker threads."""

from time import perf_counter
from uuid import UUID, uuid4
from starlette.datastructures import Headers, MutableHeaders
from enterprise_ai_ops_agent.observability import emit, error_category, request_id


def resolve_request_id(value):
    try:
        if isinstance(value, str) and len(value) == 36 and str(UUID(value)) == value.lower():
            return value
    except ValueError:
        pass
    return str(uuid4())


class RequestTelemetry:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        identity = resolve_request_id(Headers(scope=scope).get("x-request-id"))
        token = request_id.set(identity)
        start = perf_counter()
        status = 500
        response_started = False
        method = scope["method"] if scope["method"] in {"GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"} else "OTHER"
        emit("request_started", "http", method=method, route="<unresolved>")

        async def response(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = identity
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, response)
        except BaseException as error:
            # If no response started, render the same generic Starlette 500 here so
            # correlation/security headers are also present on unhandled failures.
            emit("request_failed", "http", method=method, route=getattr(scope.get("route"),"path","<unmatched>"),
                 status_code=500, duration_ms=round((perf_counter()-start)*1000,3),
                 outcome="failed", error_category=error_category(error))
            if isinstance(error, Exception) and not response_started:
                from starlette.responses import PlainTextResponse
                await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, response)
            raise
        else:
            emit("request_failed" if status >= 500 else "request_completed", "http", method=method,
                 route=getattr(scope.get("route"),"path","<unmatched>"), status_code=status,
                 duration_ms=round((perf_counter()-start)*1000,3), outcome="failed" if status >= 500 else "completed",
                 **({"error_category":"internal_error"} if status >= 500 else {}))
        finally:
            request_id.reset(token)
