#!/usr/bin/env python3
"""Build a deterministic allowlisted Skill ZIP, excluding all local state and extras."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    'LICENSE', 'README.md', 'SKILL.md', 'VALIDATION.md', 'config.example.json',
    'mcp.example.json', 'install.py', 'install.cmd', 'install.sh', 'docs/USAGE.md',
    'auto_phone/__init__.py', 'auto_phone/apps.json', 'auto_phone/contracts.py',
    'auto_phone/entry.py', 'auto_phone/phone.py', 'auto_phone/platform.py',
    'auto_phone/runtime.py', 'scripts/phone_agent.py', 'scripts/live_smoke.py',
    'scripts/package_zip.py',
)
TEST_FILES = (
    '.github/workflows/verify.yml', 'tests/test_core.py', 'tests/test_safety.py',
    'tests/test_wire.py', 'tests/test_distribution.py',
)


def build(root: Path, destination: Path) -> Path:
    root = root.resolve()
    names = []
    for name in (*RUNTIME_FILES, *TEST_FILES):
        path = root / name
        safe = (path.is_file() and not path.is_symlink() and
                path.resolve().is_relative_to(root))
        if not safe:
            if name in RUNTIME_FILES:
                raise ValueError('Required archive file is missing or unsafe: ' + name)
            continue
        names.append(name)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'auto-phone-skill-0.3.0.zip'
    temporary = target.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(names):
                info = zipfile.ZipInfo('auto-phone-skill/' + name, date_time=(2026, 9, 6, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (root / name).read_bytes())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / 'SHA256SUMS.txt').write_text(checksum + '  ' + target.name + '\n', encoding='utf-8')
    return target


def main():
    print(build(ROOT, ROOT / 'dist'))


if __name__ == '__main__':
    main()
