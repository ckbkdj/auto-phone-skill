#!/usr/bin/env python3
"""Run core checks or the stricter Docker + real private-Skill release gate.

`--core` is useful in offline development and is explicitly NOT a release approval.
The default `--release` refuses to pass without Docker and a real private Skill/Appium device.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def core_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return env


def verify_contracts() -> None:
    from lobster_phone_agent.schemas import TaskRequest

    rendered = json.dumps(TaskRequest.model_json_schema(), ensure_ascii=False).lower()
    forbidden = ("appium_url", "udid", "system_port", "capabilities", "bridge_token", "adb_host", "adb_port")
    present = [name for name in forbidden if f'"{name}"' in rendered]
    if present:
        raise RuntimeError(f"public schema leaks private device fields: {present}")
    before = {path.name: path.read_bytes() for path in (ROOT / "contracts").glob("*.json")}
    run([sys.executable, "scripts/generate_contracts.py"], env=core_env())
    after = {path.name: path.read_bytes() for path in (ROOT / "contracts").glob("*.json")}
    # Object key ordering is not semantic JSON Schema drift across Pydantic versions.
    if before and {key: json.loads(value) for key, value in before.items()} != {key: json.loads(value) for key, value in after.items()}:
        raise RuntimeError("generated contract drift: run scripts/generate_contracts.py and commit every schema")
    print("contract checks: PASS", flush=True)


def verify_topology() -> None:
    import yaml

    compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text("utf-8"))
    services = compose.get("services", {})
    if set(services) != {"phone-agent", "caddy"}:
        raise RuntimeError("public Compose must contain only the control plane and Caddy")
    if services["phone-agent"].get("ports"):
        raise RuntimeError("control-plane port 8788 must not be published directly")
    published = services["caddy"].get("ports", [])
    allowed = {"80:80", "443:443", "443:443/udp"}
    if set(published) != allowed:
        raise RuntimeError(f"unexpected public ingress ports: {published}")
    if not compose.get("networks", {}).get("control", {}).get("internal"):
        raise RuntimeError("control network must be internal")
    for path in ROOT.rglob("*.yaml"):
        if any(part in {".venv", "build", "dist", ".git"} for part in path.parts):
            continue
        yaml.safe_load(path.read_text("utf-8"))
    for path in ROOT.rglob("*.yml"):
        if any(part in {".venv", "build", "dist", ".git"} for part in path.parts):
            continue
        yaml.safe_load(path.read_text("utf-8"))
    print("topology/YAML checks: PASS", flush=True)


def verify_settings() -> None:
    from lobster_phone_agent.config import Settings
    from lobster_phone_agent.skill.models import SkillConfig

    Settings(
        production_mode=True,
        public_url="https://phone.example.com",
        api_key="a" * 32,
        bridge_token="b" * 32,
    )
    SkillConfig.from_yaml(ROOT / "skill/config.example.yaml")
    print("production/skill configuration models: PASS", flush=True)


def build_and_verify_wheel() -> Path:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    if importlib.util.find_spec("build") is not None:
        run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)])
    else:
        run([
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(dist),
        ])
    wheels = sorted(dist.glob("lobster_phone_agent-*.whl"), key=lambda item: item.stat().st_mtime)
    if not wheels:
        raise RuntimeError("wheel build produced no package")
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            "lobster_phone_agent/skill/gateway.py",
            "lobster_phone_agent/bridge/hub.py",
            "lobster_phone_agent/bridge/protocol.py",
            "lobster_phone_agent/resources/config/apps.yaml",
        }
        missing = required - names
        if missing:
            raise RuntimeError(f"wheel is missing required files: {sorted(missing)}")
    with tempfile.TemporaryDirectory(prefix="phone-agent-wheel-") as temp:
        temp_path = Path(temp)
        run([sys.executable, "-m", "venv", "--system-site-packages", str(temp_path / "venv")])
        python = temp_path / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([
            str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)
        ])
        code = r'''
from pathlib import Path
import lobster_phone_agent
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.config import Settings
from lobster_phone_agent.app import create_app
from lobster_phone_agent.api.sse import EventSourceResponse
from lobster_phone_agent.skill.gateway import create_skill_app
from lobster_phone_agent.skill.models import SkillConfig
from importlib.metadata import distribution
settings = Settings(enable_a2a=False, production_mode=False)
registry = AppRegistry.from_yaml(settings.config_dir / "apps.yaml")
recipes = RecipePlanner.from_directory(settings.config_dir / "recipes")
assert len(registry.records) >= 60
assert registry.resolve("12306").package == "com.MobileTicket"
assert len(recipes.recipes) >= 5
assert "phone-agent-wheel-" in str(Path(lobster_phone_agent.__file__).resolve())
entry_points = {entry.name: entry.value for entry in distribution("lobster-phone-agent").entry_points}
assert entry_points["lobster-phone-agent"] == "lobster_phone_agent.main:run"
assert entry_points["lobster-phone-skill"] == "lobster_phone_agent.skill.cli:run"
assert entry_points["auto-phone-control"] == "lobster_phone_agent.main:run"
assert entry_points["auto-phone-skill"] == "lobster_phone_agent.skill.cli:run"
create_app(settings)
create_skill_app(SkillConfig.model_validate({
    "server_url": "https://phone.example.com",
    "bridge_id": "wheel-test",
    "bridge_token": "bridge-token-for-wheel-test-123",
    "api_token": "public-api-token-for-wheel-123",
    "local_token": "local-skill-token-for-wheel-123",
    "devices": [{"id": "cloud-1", "appium_url": "http://127.0.0.1:4723", "udid": "emulator-5554"}],
}))
print({"module": str(Path(lobster_phone_agent.__file__).resolve()), "apps": len(registry.records), "recipes": len(recipes.recipes), "entry_points": entry_points})
'''
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PHONE_AGENT_CONFIG_DIR"] = str(
            temp_path / "venv" / "unused-config-path"
        )
        env.pop("PHONE_AGENT_CONFIG_DIR", None)
        run([str(python), "-c", code], env=env, cwd=temp_path)
    print(f"wheel checks: PASS ({wheel.name})", flush=True)
    return wheel


def docker_gate() -> None:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("RELEASE BLOCKED: Docker is unavailable; image/Compose tests were not run")
    run([docker, "info"])
    run([docker, "build", "--tag", "lobster-phone-agent-control-plane:release-gate", "."])
    run([
        docker, "run", "--rm", "--entrypoint", "python",
        "lobster-phone-agent-control-plane:release-gate", "-c",
        "from lobster_phone_agent.app import create_app; from lobster_phone_agent.config import Settings; create_app(Settings()); print('container import PASS')",
    ])
    caddyfile = str((ROOT / "deploy/Caddyfile").resolve())
    run([
        docker, "run", "--rm", "-e", "PHONE_AGENT_DOMAIN=phone.example.com",
        "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro", "caddy:2.10-alpine",
        "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
    ])
    with tempfile.TemporaryDirectory(prefix="phone-agent-compose-") as temp:
        temp_root = Path(temp)
        deploy = temp_root / "deploy"
        deploy.mkdir()
        shutil.copy2(ROOT / "deploy/docker-compose.yml", deploy / "docker-compose.yml")
        shutil.copy2(ROOT / "deploy/Caddyfile", deploy / "Caddyfile")
        shutil.copy2(ROOT / ".env.example", temp_root / ".env")
        run([docker, "compose", "-f", str(deploy / "docker-compose.yml"), "config", "--quiet"], cwd=temp_root)
    name = "phone-agent-release-gate"
    try:
        run([
            docker, "run", "--detach", "--rm", "--name", name,
            "-p", "127.0.0.1::8788",
            "-e", "PHONE_AGENT_PRODUCTION_MODE=true",
            "-e", "PHONE_AGENT_PUBLIC_URL=https://phone.example.com",
            "-e", "PHONE_AGENT_API_KEY=" + "a" * 32,
            "-e", "PHONE_AGENT_BRIDGE_TOKEN=" + "b" * 32,
            "lobster-phone-agent-control-plane:release-gate",
        ])
        mapping = subprocess.check_output([docker, "port", name, "8788/tcp"], text=True).strip().splitlines()[0]
        port = mapping.rsplit(":", 1)[1]
        code = (
            "import urllib.request; "
            f"print(urllib.request.urlopen('http://127.0.0.1:{port}/healthz', timeout=3).read().decode())"
        )
        for attempt in range(30):
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            if result.returncode == 0:
                print(result.stdout, end="")
                break
            time.sleep(1)
        else:
            raise RuntimeError("container health endpoint never became ready")
    finally:
        subprocess.run([docker, "rm", "-f", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Docker/Caddy checks: PASS", flush=True)


def live_gate() -> None:
    required = ("PHONE_SKILL_URL", "PHONE_SKILL_TOKEN", "PHONE_SKILL_DEVICE_ID")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "RELEASE BLOCKED: real private Skill/Appium smoke test is required; missing "
            + ", ".join(missing)
        )
    run([sys.executable, "scripts/live_smoke.py"], env=core_env())


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--core", action="store_true", help="offline core checks only; not a release approval")
    mode.add_argument("--release", action="store_true", help="strict Docker + live private Skill release gate (default)")
    args = parser.parse_args()
    strict = not args.core
    sys.path.insert(0, str(ROOT / "src"))
    ARTIFACTS.mkdir(exist_ok=True)
    report: dict[str, object] = {"mode": "release" if strict else "core", "status": "running"}
    output = ARTIFACTS / ("release-gate.json" if strict else "core-gate.json")
    try:
        run([sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts"])
        run([
            sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "--cov=lobster_phone_agent",
            "--cov-report=term-missing:skip-covered", "--cov-report=json:artifacts/coverage.json",
            "--cov-fail-under=70",
        ], env=core_env())
        run([sys.executable, "scripts/validate_recipes.py"], env=core_env())
        verify_contracts()
        verify_topology()
        verify_settings()
        run([sys.executable, "scripts/benchmark.py"], env=core_env())
        wheel = build_and_verify_wheel()
        report["wheel"] = wheel.name
        if strict:
            docker_gate()
            live_gate()
        report["status"] = "passed"
        report["release_approved"] = strict
        print("STRICT RELEASE GATE: PASS" if strict else "CORE GATE: PASS (not a release approval)", flush=True)
        return 0
    except Exception as exc:
        report["status"] = "blocked" if "RELEASE BLOCKED" in str(exc) else "failed"
        report["detail"] = str(exc)
        report["release_approved"] = False
        print(f"GATE FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"gate report: {output}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
