from __future__ import annotations

import argparse
import ipaddress
import logging
from pathlib import Path

import uvicorn

from lobster_phone_agent.skill.gateway import create_skill_app
from lobster_phone_agent.skill.models import SkillConfig


def _loopback_only(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private Lobster phone skill bridge")
    parser.add_argument("--config", type=Path, required=True, help="Private skill YAML config")
    parser.add_argument("--log-level", default="INFO")
    return parser


def run() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = SkillConfig.from_yaml(args.config)
    if not _loopback_only(config.local_host):
        raise SystemExit(
            "refusing to bind private Lobster skill to a non-loopback address; "
            "use 127.0.0.1, ::1, or localhost"
        )
    uvicorn.run(
        create_skill_app(config),
        host=config.local_host,
        port=config.local_port,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    run()
