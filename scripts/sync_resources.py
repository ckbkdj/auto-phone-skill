#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "config"
    target = root / "src/lobster_phone_agent/resources/config"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print(f"synced {source} -> {target}")


if __name__ == "__main__":
    main()
