from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lobster_phone_agent.util.text import compact_text


@dataclass(slots=True, frozen=True)
class RouteDecision:
    intent: str
    confidence: float
    slots: dict[str, str] = field(default_factory=dict)
    source: str = "rules"


_RULES: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "ride_hailing",
        re.compile(r"(?:打车|叫车|网约车).*(?:去|到)|(?:美团|滴滴|高德).*(?:打车|叫车)"),
        0.98,
    ),
    (
        "navigation",
        re.compile(r"(?:导航|路线|怎么去|带我去).+"),
        0.96,
    ),
    (
        "app_search",
        re.compile(r"(?:在|打开).+?(?:搜索|搜|查找).+"),
        0.93,
    ),
    (
        "launch_app",
        re.compile(r"^(?:请)?(?:帮我)?(?:打开|启动|进入).+"),
        0.94,
    ),
    (
        "message",
        re.compile(r"(?:给|向).+?(?:发送|发消息|回复).+"),
        0.91,
    ),
    (
        "shopping",
        re.compile(r"(?:购买|下单|加入购物车|买).+"),
        0.90,
    ),
    (
        "food_delivery",
        re.compile(r"(?:点外卖|订餐|点餐|外卖).+"),
        0.90,
    ),
)


class LocalIntentRouter:
    """Sub-millisecond rules by default, optional ONNX classifier when model files exist."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self.model_dir = model_dir
        self._session: Any | None = None
        self._tokenizer: Any | None = None
        self._labels: list[str] = []
        if model_dir:
            self._load_onnx(model_dir)

    def route(self, text: str) -> RouteDecision:
        normalized = compact_text(text)
        if self._session is not None and self._tokenizer is not None:
            result = self._route_onnx(text)
            if result.confidence >= 0.72:
                return result
        for intent, pattern, confidence in _RULES:
            match = pattern.search(normalized)
            if match:
                return RouteDecision(intent=intent, confidence=confidence, source="rules")
        return RouteDecision(intent="general_ui", confidence=0.45, source="rules")

    def _load_onnx(self, model_dir: Path) -> None:
        model_path = model_dir / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        labels_path = model_dir / "labels.json"
        if not (model_path.exists() and tokenizer_path.exists() and labels_path.exists()):
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ModuleNotFoundError:
            return
        self._session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        self._labels = [str(item) for item in labels]

    def _route_onnx(self, text: str) -> RouteDecision:
        import numpy as np

        encoded = self._tokenizer.encode(text)
        ids = encoded.ids[:64]
        attention = [1] * len(ids)
        while len(ids) < 64:
            ids.append(0)
            attention.append(0)
        inputs = {
            self._session.get_inputs()[0].name: np.asarray([ids], dtype=np.int64),
            self._session.get_inputs()[1].name: np.asarray([attention], dtype=np.int64),
        }
        logits = self._session.run(None, inputs)[0][0]
        shifted = logits - logits.max()
        probabilities = np.exp(shifted) / np.exp(shifted).sum()
        index = int(probabilities.argmax())
        label = self._labels[index] if index < len(self._labels) else "general_ui"
        return RouteDecision(
            intent=label,
            confidence=float(probabilities[index]),
            source="onnx",
        )
