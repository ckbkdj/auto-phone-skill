#!/usr/bin/env python3
"""Compatibility wrapper around the standard PEP 517 wheel builder."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", str(root)],
        cwd=root,
        check=True,
    )


if __name__ == "__main__":
    main()
