#!/usr/bin/env python3
"""Global ZIP installation alias; reuse the canonical Skill installer, no pip or Git."""
from pathlib import Path
import runpy
import sys


def main():
    entry = Path(__file__).resolve().parent / 'scripts' / 'phone_agent.py'
    if not entry.is_file():
        print('{"version":"1.0","ok":false,"code":"INCOMPLETE_SKILL_ARCHIVE"}')
        return 1
    sys.argv = [str(entry), 'install', *sys.argv[1:]]
    runpy.run_path(str(entry), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
