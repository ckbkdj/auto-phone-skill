"""Test the exact ZIP/global installation path, not an Appium/Android certification."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skill_zip_builder', ROOT / 'scripts/package_zip.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class Distribution(unittest.TestCase):
    def test_root_install_alias_from_another_directory_without_site_packages(self):
        with tempfile.TemporaryDirectory(prefix='phone install ') as tmp:
            skills = Path(tmp) / 'global skills'
            process = subprocess.run([sys.executable, '-I', '-S', str(ROOT / 'install.py'),
                                      '--skills-dir', str(skills)], cwd=tmp, capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertTrue((skills / 'auto-phone-skill/SKILL.md').is_file())
            self.assertTrue((skills / 'auto-phone-skill/auto_phone/runtime.py').is_file())

    def test_zip_uses_allowlist_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('dist', '__pycache__', '.git'))
            for name in ('phone-private.json', '.env', 'random-script.py', 'config.local.json'):
                (source / name).write_text('THIS MUST NEVER BE DISTRIBUTED')
            (source / 'backup').mkdir()
            (source / 'backup/credentials.json').write_text('PRIVATE')
            archive = builder.build(source, Path(tmp) / 'dist')
            original = archive.read_bytes()
            self.assertEqual(original, builder.build(source, Path(tmp) / 'dist').read_bytes())
            with zipfile.ZipFile(archive) as z:
                self.assertIsNone(z.testzip())
                self.assertIn('auto-phone-skill/install.py', z.namelist())
                self.assertFalse(any('private' in n or 'backup' in n or 'random-script' in n for n in z.namelist()))
                self.assertFalse(any(b'THIS MUST NEVER' in z.read(n) for n in z.namelist() if '/tests/' not in n))

    def test_exact_archive_extraction_runs_without_pip_or_project_working_directory(self):
        with tempfile.TemporaryDirectory(prefix='phone zip ') as tmp:
            archive = builder.build(ROOT, Path(tmp) / 'dist')
            destination = Path(tmp) / 'extracted'
            with zipfile.ZipFile(archive) as z:
                z.extractall(destination)
            script = destination / 'auto-phone-skill/scripts/phone_agent.py'
            env = dict(os.environ, AUTO_PHONE_HOME=str(Path(tmp) / 'state'),
                       AUTO_PHONE_CONFIG=str(Path(tmp) / 'absent.json'))
            process = subprocess.run([sys.executable, '-I', '-S', str(script), 'doctor', '--json', '{}'],
                                     cwd=tmp, env=env, capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            answer = json.loads(process.stdout)
            self.assertTrue(answer['ok'])
            self.assertEqual(answer['report']['runtime_dependencies'], 0)

    def test_missing_required_source_refuses_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'Required archive file'):
                builder.build(Path(tmp), Path(tmp) / 'dist')


if __name__ == '__main__':
    unittest.main()
