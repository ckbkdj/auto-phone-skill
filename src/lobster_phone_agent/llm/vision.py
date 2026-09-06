from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import httpx
from pydantic import Field, StrictBool, StrictFloat, StrictStr

from lobster_phone_agent.contracts import ContractModel, strict_json_object

from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.schemas import SemanticTarget


class VisionGroundingResponse(ContractModel):
    found: StrictBool
    confidence: StrictFloat = Field(ge=0, le=1)
    bbox: tuple[StrictFloat, StrictFloat, StrictFloat, StrictFloat] | None = None
    label: StrictStr = Field(default="", max_length=256)
    reason: StrictStr = Field(default="", max_length=512)


@dataclass(slots=True, frozen=True)
class VisionPoint:
    x: int
    y: int
    confidence: StrictFloat
    reason: str


class VisionGrounder:
    """Slow-path screenshot grounding. It never plans a workflow or approves risk."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.vision_timeout_seconds),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.vision_fallback_enabled
            and (self.settings.vision_model or self.settings.llm_model)
        )

    async def ground(
        self,
        *,
        png: bytes,
        snapshot: ScreenSnapshot,
        target: SemanticTarget,
    ) -> VisionPoint | None:
        if not self.enabled:
            return None
        model = self.settings.vision_model or self.settings.llm_model
        prompt = {
            "target": target.model_dump(mode="json", exclude_none=True),
            "screen_size": [snapshot.width, snapshot.height],
            "package": snapshot.package,
            "instruction": (
                "Find exactly one visible UI control matching target. Return normalized bbox "
                "[x1,y1,x2,y2] in 0..1. Do not infer hidden controls. found=false when uncertain."
            ),
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a conservative Android screenshot grounder. Only locate the "
                        "requested visible control. Never reason about passwords, CAPTCHA, "
                        "payment, "
                        "or authorization. Reply as JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(png).decode("ascii")
                            },
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self._client.post("chat/completions", json=payload)
            response.raise_for_status()
            choice = strict_json_object(response.content)["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                return None
            content = choice["message"]["content"]
            result = VisionGroundingResponse.model_validate(strict_json_object(content))
        except Exception:
            return None
        if (
            not result.found
            or result.confidence < self.settings.vision_min_confidence
            or result.bbox is None
        ):
            return None
        x1, y1, x2, y2 = result.bbox
        if not all(0 <= value <= 1 for value in result.bbox):
            return None
        if x2 <= x1 or y2 <= y1 or (x2 - x1) * (y2 - y1) > 0.45:
            return None
        x = int(((x1 + x2) / 2) * snapshot.width)
        y = int(((y1 + y2) / 2) * snapshot.height)
        return VisionPoint(
            x=x,
            y=y,
            confidence=result.confidence,
            reason=result.reason or result.label,
        )

    async def close(self) -> None:
        await self._client.aclose()
