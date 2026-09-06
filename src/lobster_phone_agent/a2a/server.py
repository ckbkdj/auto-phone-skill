from __future__ import annotations

import secrets

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from lobster_phone_agent import __version__
from lobster_phone_agent.a2a.adapter import PhoneControlA2AExecutor
from lobster_phone_agent.agent.service import TaskService
from lobster_phone_agent.config import Settings


class A2AApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, api_key: str | None) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request, call_next):
        if not self.api_key or "/.well-known/" in request.url.path:
            return await call_next(request)
        supplied = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not secrets.compare_digest(supplied, self.api_key):
            return JSONResponse({"detail": "invalid API key"}, status_code=401)
        return await call_next(request)


def create_a2a_app(settings: Settings, service: TaskService) -> Starlette:
    skill = AgentSkill(
        id="semantic_android_control",
        name="Semantic Android Control",
        description=(
            "Turn a natural-language phone goal into a compact semantic action plan and execute "
            "it on a caller-selected private-Skill device ID with confirmation and handoff gates."
        ),
        input_modes=["application/json", "text/plain"],
        output_modes=["application/json"],
        tags=["android", "appium", "phone-control", "openclaw"],
        examples=[
            '{"instruction":"打开美团","device":{"id":"cloud-1","bridge_id":"lobster-home"}}',
            '{"instruction":"用美团打车去北京南站","device":{"id":"cloud-1","bridge_id":"lobster-home"}}',
        ],
    )
    base_url = settings.public_url.rstrip("/") + "/a2a"
    card = AgentCard(
        name="Lobster Phone Agent",
        description="A safety-gated semantic Android control sub-agent for Lobster/OpenClaw.",
        version=__version__,
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=base_url,
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )
    handler = DefaultRequestHandler(
        agent_executor=PhoneControlA2AExecutor(
            service,
            wait_timeout_seconds=settings.a2a_wait_timeout_seconds,
        ),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")]
    middleware = [Middleware(A2AApiKeyMiddleware, api_key=settings.api_key)]
    return Starlette(routes=routes, middleware=middleware)
