from __future__ import annotations

import asyncio
import ipaddress
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path as ApiPath, Query, status
from pydantic import ValidationError

from lobster_phone_agent.schemas import (
    ConfirmationRequest, EmptyRequest, HandoffResumeRequest, LocalTaskRequest, SkillHealthResponse, TaskRecord, TaskRequest,
)
from lobster_phone_agent.contracts import strict_json_object
from lobster_phone_agent.api.contracts import ERROR_RESPONSES, install_contract_handlers

TaskId = Annotated[str, ApiPath(pattern=r"^[a-f0-9]{32}$")]
from lobster_phone_agent.skill.client import SkillBridgeClient
from lobster_phone_agent.skill.dispatcher import SkillRpcDispatcher
from lobster_phone_agent.skill.models import SkillConfig
from lobster_phone_agent.skill.pool import LocalAppiumPool

_FORBIDDEN_LOBSTER_FIELDS = {
    "device",
    "bridge_id",
    "appium_url",
    "udid",
    "device_name",
    "system_port",
    "capabilities",
    "bridge_token",
    "adb_host",
    "adb_port",
}


class ControlPlaneClient:
    def __init__(self, config: SkillConfig) -> None:
        self._bridge_id = config.bridge_id
        self._device_ids = {d.id for d in config.devices}
        self._client = httpx.AsyncClient(
            base_url=config.server_url,
            headers={"Authorization": f"Bearer {config.api_token}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> Any:
        if method == "POST" and path.startswith("/v1/tasks/"):
            # Read and verify ownership BEFORE forwarding confirm/resume/cancel.
            await self.request("GET", path.rsplit("/", 1)[0])
        try:
            kwargs = {"json": json}
            if timeout is not None:
                kwargs["timeout"] = timeout
            async with self._client.stream(method, path, **kwargs) as upstream:
                body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise HTTPException(status_code=502, detail="output contract limit exceeded")
                response = httpx.Response(upstream.status_code, content=bytes(body), request=upstream.request)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"public control plane is unavailable: {type(exc).__name__}",
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code,
                detail="public control plane rejected the request",
            )
        try:
            data = strict_json_object(response.content)
            record = TaskRecord.model_validate(data)
            if record.request.device.bridge_id != self._bridge_id or record.request.device.id not in self._device_ids:
                raise HTTPException(status_code=403, detail="task belongs to a different private Skill")
            return record.model_dump(mode="json")
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="public control plane returned invalid JSON",
            ) from exc


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build_task_request(
    payload: dict[str, Any],
    *,
    config: SkillConfig,
    device_ids: set[str],
) -> TaskRequest:
    body = dict(payload)
    forbidden = sorted(_FORBIDDEN_LOBSTER_FIELDS.intersection(body))
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Lobster may only send device_id; private routing fields are forbidden: "
                + ", ".join(forbidden)
            ),
        )
    try:
        local = LocalTaskRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid local task contract") from exc
    body = local.model_dump(mode="json")
    device_id = body.pop("device_id")
    if device_id not in device_ids:
        raise HTTPException(status_code=404, detail="unknown local device_id")
    body["device"] = {"id": device_id, "bridge_id": config.bridge_id}
    try:
        return TaskRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid public task contract",
        ) from exc


def create_skill_app(config: SkillConfig) -> FastAPI:
    if not _is_loopback(config.local_host):
        raise ValueError(
            "private Lobster skill must bind only to 127.0.0.1, ::1, or localhost"
        )

    pool = LocalAppiumPool(config.devices, config.runtime)
    dispatcher = SkillRpcDispatcher(pool)
    bridge = SkillBridgeClient(config, dispatcher)
    public = ControlPlaneClient(config)
    device_ids = set(pool.device_ids)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bridge_task = asyncio.create_task(bridge.run_forever(), name="private-skill-bridge")
        reaper_task = asyncio.create_task(
            _reaper(pool, config.runtime.reaper_interval_seconds),
            name="private-appium-reaper",
        )
        try:
            yield
        finally:
            await bridge.stop()
            bridge_task.cancel()
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await bridge_task
            with suppress(asyncio.CancelledError):
                await reaper_task
            await pool.close_all()
            await public.close()

    app = FastAPI(
        title="Auto Phone Skill Gateway",
        responses=ERROR_RESPONSES,
        description="Private loopback gateway. Never expose this listener to the Internet.",
        lifespan=lifespan,
    )

    async def local_auth(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        candidate = x_api_key
        if authorization and authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
        if not candidate or not secrets.compare_digest(candidate, config.local_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid local skill token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    protected = [Depends(local_auth)]

    @app.get("/healthz", response_model=SkillHealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "bridge_connected": bridge.connected.is_set(),
            "bridge_id": config.bridge_id,
            "devices": pool.device_ids,
            "scope": "loopback-only",
        }

    @app.post("/v1/tasks", response_model=TaskRecord, dependencies=protected)
    async def create_task(payload: LocalTaskRequest) -> Any:
        request = _build_task_request(
            payload.model_dump(mode="json"),
            config=config,
            device_ids=device_ids,
        )
        return await public.request(
            "POST",
            "/v1/tasks",
            json=request.model_dump(mode="json"),
        )

    @app.post("/v1/execute", response_model=TaskRecord, dependencies=protected)
    async def execute_task(payload: LocalTaskRequest, wait_seconds: float = Query(default=30, ge=0, le=600, allow_inf_nan=False)) -> Any:
        request = _build_task_request(
            payload.model_dump(mode="json"),
            config=config,
            device_ids=device_ids,
        )
        bounded_wait = max(0.0, min(wait_seconds, 600.0))
        # The public endpoint may legitimately hold the response for the requested wait.
        # A fixed 30-second client timeout caused the private Skill to abort otherwise
        # healthy tasks at the exact default boundary.
        request_timeout = httpx.Timeout(
            connect=10.0,
            read=max(30.0, bounded_wait + 15.0),
            write=30.0,
            pool=10.0,
        )
        return await public.request(
            "POST",
            f"/v1/execute?wait_seconds={bounded_wait}",
            json=request.model_dump(mode="json"),
            timeout=request_timeout,
        )

    @app.get("/v1/tasks/{task_id}", response_model=TaskRecord, dependencies=protected)
    async def get_task(task_id: TaskId) -> Any:
        return await public.request("GET", f"/v1/tasks/{task_id}")

    @app.post("/v1/tasks/{task_id}/confirm", response_model=TaskRecord, dependencies=protected)
    async def confirm(task_id: TaskId, payload: ConfirmationRequest) -> Any:
        return await public.request(
            "POST",
            f"/v1/tasks/{task_id}/confirm",
            json=payload.model_dump(mode="json"),
        )

    @app.post("/v1/tasks/{task_id}/resume", response_model=TaskRecord, dependencies=protected)
    async def resume(task_id: TaskId, payload: HandoffResumeRequest) -> Any:
        return await public.request(
            "POST",
            f"/v1/tasks/{task_id}/resume",
            json=payload.model_dump(mode="json"),
        )

    @app.post("/v1/tasks/{task_id}/cancel", response_model=TaskRecord, dependencies=protected)
    async def cancel(task_id: TaskId, payload: EmptyRequest | None = None) -> Any:
        return await public.request("POST", f"/v1/tasks/{task_id}/cancel")

    install_contract_handlers(app)
    app.state.bridge = bridge
    app.state.pool = pool
    app.state.config = config
    app.state.public_client = public
    return app


async def _reaper(pool: LocalAppiumPool, interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await pool.reap_idle()
