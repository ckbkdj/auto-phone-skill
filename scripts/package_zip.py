#!/usr/bin/env python3
"""Build a transparent installable archive, with no credentials or generated state."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '__pycache__', '.pytest_cache', 'dist', 'artifacts', '.venv'}

def main():
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    target = output / 'auto-phone-skill-0.3.0.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob('*')):
            relative = path.relative_to(ROOT)
            if not path.is_file() or path.is_symlink() or any(x in SKIP for x in relative.parts):
                continue
            if path.name in {'.env', 'config.json', '.coverage'} or path.suffix in {'.pyc', '.log', '.sqlite3'} or path.name.endswith('.local.json'):
                continue
            archive.write(path, 'auto-phone-skill/' + relative.as_posix())
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(checksum + '  ' + target.name + '\n')
    print(target)

if __name__ == '__main__':
    main()
