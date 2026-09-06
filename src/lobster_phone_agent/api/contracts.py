"""Bounded HTTP JSON parsing and uniform, non-echoing error output."""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from pydantic import Field, StrictStr
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from lobster_phone_agent.contracts import ContractModel, MAX_REQUEST_BYTES, strict_json_object
from lobster_phone_agent.errors import InvalidTaskState, PhoneAgentError, TaskNotFound


class Violation(ContractModel):
    field: StrictStr = Field(max_length=256)
    rule: StrictStr = Field(max_length=128)


class ErrorDetail(ContractModel):
    code: Literal[
        "INVALID_JSON", "INVALID_REQUEST", "INVALID_OUTPUT", "UNAUTHORIZED", "FORBIDDEN",
        "NOT_FOUND", "CONFLICT", "REQUEST_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE",
        "UPSTREAM_ERROR", "EXECUTION_ERROR", "INTERNAL_ERROR", "REQUEST_TIMEOUT",
    ]
    message: StrictStr = Field(max_length=256)
    request_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    violations: list[Violation] = Field(default_factory=list, max_length=32)


class ErrorResponse(ContractModel):
    version: Literal["1.0"] = "1.0"
    error: ErrorDetail


_MESSAGES = {
    "INVALID_JSON": "Body must be one valid JSON object without duplicate keys.",
    "INVALID_REQUEST": "Request does not match the input contract.",
    "INVALID_OUTPUT": "Upstream output does not match the response contract.",
    "UNAUTHORIZED": "Valid authentication is required.",
    "FORBIDDEN": "This operation is forbidden.",
    "NOT_FOUND": "The requested resource was not found.",
    "CONFLICT": "The request conflicts with task state or idempotency identity.",
    "REQUEST_TOO_LARGE": "Request body exceeds 262144 bytes.",
    "UNSUPPORTED_MEDIA_TYPE": "Use uncompressed application/json encoded as UTF-8.",
    "UPSTREAM_ERROR": "The upstream service is unavailable.",
    "EXECUTION_ERROR": "The operation could not be completed.",
    "INTERNAL_ERROR": "An internal error prevented completion.",
    "REQUEST_TIMEOUT": "Request body deadline exceeded.",
}


def error_response(code: str, status: int, request_id: str, violations=None, headers=None):
    body = ErrorResponse(error=ErrorDetail(
        code=code, message=_MESSAGES[code], request_id=request_id, violations=violations or [],
    ))
    return JSONResponse(
        status_code=status, content=body.model_dump(mode="json"),
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store", **(headers or {})},
    )


def safe_violations(errors: list[dict[str, Any]]) -> list[Violation]:
    # Do not echo attacker-supplied dictionary keys, rejected values, or validator contexts.
    output = []
    for item in errors[:32]:
        loc = list(item.get("loc", ()))
        if item.get("type") == "extra_forbidden" and loc:
            loc[-1] = "<extra>"
        output.append(Violation(field=".".join(str(x) for x in loc)[:256],
                                rule=str(item.get("type", "validation_error"))[:128]))
    return output


class JsonContractMiddleware:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["contract_request_id"] = request_id
        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        async def fail(code: str, status: int) -> None:
            await error_response(code, status, request_id)(scope, receive, send)
        if headers.get(b"content-encoding", b"identity").lower() not in {b"identity", b""}:
            await fail("UNSUPPORTED_MEDIA_TYPE", 415)
            return
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if not length.isdigit():
                    raise ValueError("invalid length")
                if int(length) > self.max_bytes:
                    await fail("REQUEST_TOO_LARGE", 413)
                    return
            except ValueError:
                await fail("INVALID_JSON", 400)
                return
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > self.max_bytes:
                        await fail("REQUEST_TOO_LARGE", 413)
                        return
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            await fail("REQUEST_TIMEOUT", 408)
            return
        if body:
            content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content_type != b"application/json":
                await fail("UNSUPPORTED_MEDIA_TYPE", 415)
                return
            try:
                strict_json_object(bytes(body), max_bytes=self.max_bytes)
            except (ValueError, UnicodeError):
                await fail("INVALID_JSON", 400)
                return
        sent = False
        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        await self.app(scope, replay, send)


def install_contract_handlers(app: FastAPI) -> None:
    app.add_middleware(JsonContractMiddleware)
    def rid(request: Request) -> str:
        return getattr(request.state, "contract_request_id", uuid4().hex)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return error_response("INVALID_REQUEST", 422, rid(request), safe_violations(exc.errors()))

    @app.exception_handler(ResponseValidationError)
    async def invalid_output(request: Request, _exc: ResponseValidationError):
        return error_response("INVALID_OUTPUT", 502, rid(request))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        codes = {400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 409: "CONFLICT", 413: "REQUEST_TOO_LARGE", 415: "UNSUPPORTED_MEDIA_TYPE",
                 422: "INVALID_REQUEST", 502: "UPSTREAM_ERROR", 503: "UPSTREAM_ERROR"}
        return error_response(codes.get(exc.status_code, "EXECUTION_ERROR"), exc.status_code,
                              rid(request), headers=exc.headers)

    @app.exception_handler(TaskNotFound)
    async def missing(request: Request, _exc: TaskNotFound):
        return error_response("NOT_FOUND", 404, rid(request))

    @app.exception_handler(InvalidTaskState)
    async def conflict(request: Request, _exc: InvalidTaskState):
        return error_response("CONFLICT", 409, rid(request))

    @app.exception_handler(PhoneAgentError)
    async def execution_error(request: Request, _exc: PhoneAgentError):
        return error_response("EXECUTION_ERROR", 400, rid(request))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, _exc: Exception):
        return error_response("INTERNAL_ERROR", 500, rid(request))


ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (
    400, 401, 403, 404, 408, 409, 413, 415, 422, 500, 502, 503,
)}
