#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
from pathlib import Path
from time import perf_counter

from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.device.ui import parse_uiautomator_xml
from lobster_phone_agent.edge.router import LocalIntentRouter
from lobster_phone_agent.schemas import DeviceDescriptor, SemanticTarget, TaskRequest


def benchmark(name: str, callback, iterations: int = 1000) -> dict[str, object]:
    samples = []
    for _ in range(iterations):
        started = perf_counter()
        callback()
        samples.append((perf_counter() - started) * 1000)
    samples.sort()
    return {
        "name": name,
        "iterations": iterations,
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[int(len(samples) * 0.95)], 4),
        "p99_ms": round(samples[int(len(samples) * 0.99)], 4),
        "max_ms": round(max(samples), 4),
    }


def main() -> None:
    router = LocalIntentRouter()
    recipes = RecipePlanner.from_directory(Path("config/recipes"))
    request = TaskRequest(
        instruction="用美团打车去北京南站",
        device=DeviceDescriptor(id="bench", bridge_id="benchmark-bridge"),
    )
    xml_nodes = "".join(
        f'<node text="项目{index}" class="android.widget.TextView" package="com.example" '
        f'content-desc="" resource-id="com.example:id/item_{index}" clickable="true" '
        f'enabled="true" bounds="[0,{index * 30}][600,{index * 30 + 28}]"/>'
        for index in range(120)
    )
    snapshot = parse_uiautomator_xml(f"<hierarchy>{xml_nodes}</hierarchy>")
    matcher = SemanticMatcher()
    target = SemanticTarget(text="项目98")
    results = [
        benchmark("local_intent_router", lambda: router.route(request.instruction)),
        benchmark("recipe_planner", lambda: recipes.plan(request)),
        benchmark("semantic_match_120_nodes", lambda: matcher.best(snapshot, target)),
        benchmark("compact_screen_120_nodes", lambda: snapshot.compact(max_nodes=80)),
    ]
    output = {
        "note": (
            "These are local CPU-only microbenchmarks, not end-to-end phone timings. "
            "Appium transport, UI rendering, and remote LLM latency are excluded."
        ),
        "results": results,
    }
    Path("benchmarks").mkdir(exist_ok=True)
    Path("benchmarks/latest.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
