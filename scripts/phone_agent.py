#!/usr/bin/env python3
"""Self-contained launcher; the installed Skill directory supplies all Python modules."""
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    print('{"version":"1.0","ok":false,"code":"PYTHON_311_REQUIRED"}', file=sys.stderr if 'mcp' in sys.argv else sys.stdout)
    raise SystemExit(1)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_phone.entry import main

if __name__ == '__main__':
    raise SystemExit(main())
