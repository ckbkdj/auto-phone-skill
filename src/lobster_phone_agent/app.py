from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, status

from lobster_phone_agent import __version__
from lobster_phone_agent.agent.conditions import ConditionEvaluator
from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.agent.stepwise import StepwiseExecutor
from lobster_phone_agent.llm.next_action import NextActionPlanner
from lobster_phone_agent.agent.risk import RiskEngine
from lobster_phone_agent.agent.service import TaskService
from lobster_phone_agent.agent.store import InMemoryTaskStore
from lobster_phone_agent.api.auth import make_auth_dependency
from lobster_phone_agent.api.routes import create_router, install_error_handlers
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.bridge.hub import BridgeHub
from lobster_phone_agent.config import Settings, get_settings
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.device.pool import DeviceSessionPool
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.llm.planner import HybridPlanner
from lobster_phone_agent.llm.vision import VisionGrounder


def build_service(
    settings: Settings,
) -> tuple[TaskService, AppRegistry, OpenAICompatibleClient, VisionGrounder, BridgeHub]:
    registry = AppRegistry.from_yaml(settings.config_dir / "apps.yaml")
    recipes = RecipePlanner.from_directory(settings.config_dir / "recipes")
    llm = OpenAICompatibleClient(settings)
    matcher = SemanticMatcher()
    conditions = ConditionEvaluator(matcher, poll_ms=settings.condition_poll_ms)
    dialogs = CommonDialogHandler(matcher)
    planner = HybridPlanner(
        settings=settings,
        recipes=recipes,
        registry=registry,
        llm=llm,
    )
    hub = BridgeHub(settings)
    pool = DeviceSessionPool(hub)
    store = InMemoryTaskStore()
    vision_grounder = VisionGrounder(settings)
    executor = StepwiseExecutor(
        settings=settings,
        planner=NextActionPlanner(settings, registry, llm),
        matcher=matcher,
        conditions=conditions,
        dialogs=dialogs,
        risk=RiskEngine(),
    )
    service = TaskService(
        settings=settings,
        store=store,
        pool=pool,
        planner=planner,
        executor=executor,
    )
    return service, registry, llm, vision_grounder, hub


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    service, registry, llm, vision_grounder, hub = build_service(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await service.shutdown()
            await llm.close()
            await vision_grounder.close()

    app = FastAPI(
        title="Lobster Phone Agent Control Plane",
        version=__version__,
        description=(
            "Public semantic Android control plane. Real Appium sessions remain inside a private "
            "Lobster skill that connects outward over an authenticated WebSocket."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.phone_service = service
    app.state.app_registry = registry
    app.state.bridge_hub = hub
    auth = make_auth_dependency(settings)
    app.include_router(
        create_router(
            settings=settings,
            service=service,
            registry=registry,
            hub=hub,
            auth_dependency=auth,
        )
    )
    install_error_handlers(app)

    @app.websocket("/v1/bridge/ws/{bridge_id}")
    async def private_skill_bridge(websocket: WebSocket, bridge_id: str) -> None:
        expected = settings.token_for_bridge(bridge_id)
        supplied = websocket.headers.get("authorization", "")
        token = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
        import secrets

        if not expected or not token or not secrets.compare_digest(token, expected):
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="invalid private bridge credentials",
            )
            return
        await hub.serve(websocket, bridge_id=bridge_id)

    if settings.enable_a2a:
        try:
            from lobster_phone_agent.a2a.server import create_a2a_app
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "A2A support is enabled but a2a-sdk is not installed; "
                "run `pip install 'lobster-phone-agent[a2a]'`"
            ) from exc
        app.mount("/a2a", create_a2a_app(settings, service))

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": "lobster-phone-agent-control-plane",
            "version": __version__,
            "docs": "/docs",
            "health": "/healthz",
            "bridge": "/v1/bridge/ws/{bridge_id}",
            "a2a": "/a2a/.well-known/agent-card.json" if settings.enable_a2a else "disabled",
        }

    return app
