from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.rate_limit import DECISION_STATE_KEY, rate_limit_headers


class ResponseHeadersMiddleware:
    """Adds per-request headers to every HTTP response, including error responses.

    - X-RateLimit-* from the decision the rate-limit dependency stored in request.state.
      (Done here rather than in the dependency because some routes return a Response
      object directly, which bypasses headers set on FastAPI's injected Response.)
    - X-Served-By with the replica id, when enabled (development aid).

    Pure ASGI middleware: it only edits the response-start message, never the body.
    """

    def __init__(self, app: ASGIApp, *, replica_id: str | None) -> None:
        self._app = app
        self._replica_id = replica_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra: dict[str, str] = {}
                decision = scope.get("state", {}).get(DECISION_STATE_KEY)
                if decision is not None:
                    extra.update(rate_limit_headers(decision))
                if self._replica_id is not None:
                    extra["X-Served-By"] = self._replica_id
                for name, value in extra.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)
