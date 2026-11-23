"""ASGI middleware.

Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so it is safe for the
streaming responses (Server-Sent Events) introduced in later phases.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from documind.core.logging import bind_request_id, reset_request_id

logger = logging.getLogger("documind.access")

REQUEST_ID_HEADER = "X-Request-ID"
# Accept client-supplied IDs only if they are short and log-safe.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_QUIET_PATH_PREFIXES = ("/api/v1/health",)


class RequestContextMiddleware:
    """Assigns a request ID, echoes it in the response and logs each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, REQUEST_ID_HEADER.lower().encode())
        request_id = (
            incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        )
        token = bind_request_id(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("Unhandled exception while processing request")
            if response_started:
                raise
            # Never leak stack traces or internal messages to clients.
            await _send_json_error(send_wrapper, 500, "Internal server error", request_id)
        finally:
            path: str = scope.get("path", "")
            level = (
                logging.DEBUG
                if path.startswith(_QUIET_PATH_PREFIXES) and status_code < 500
                else logging.INFO
            )
            logger.log(
                level,
                "%s %s %s",
                scope.get("method"),
                path,
                status_code,
                extra={
                    "http_method": scope.get("method"),
                    "http_path": path,
                    "http_status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            reset_request_id(token)


async def _send_json_error(send: Send, status: int, detail: str, request_id: str) -> None:
    body = json.dumps({"detail": detail, "request_id": request_id}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject request bodies above ``max_bytes`` with 413.

    Checks Content-Length up front and counts streamed bytes for chunked
    bodies, so an oversized upload is cut off before it fills the spool disk.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send)
            return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            if not exceeded:  # the app's own error response is replaced by 413
                await send(message)

        with contextlib.suppress(_BodyTooLargeError):
            await self.app(scope, limited_receive, guarded_send)
        if exceeded:
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {"detail": "The request body is too large.", "code": "payload_too_large"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return str(value.decode("latin-1"))
            except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any bytes
                return None
    return None
