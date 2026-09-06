from __future__ import annotations

import logging

import uvicorn

from lobster_phone_agent.app import create_app
from lobster_phone_agent.config import get_settings


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        ws_max_size=settings.bridge_max_message_bytes,
    )


if __name__ == "__main__":
    run()
