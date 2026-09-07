"""Private configuration, kernel file locks, and bounded HTTP; no global server."""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .contracts import Fault, MAX_RESPONSE, dumps, loads


class Lock:
    """Nonblocking cross-process lock released by the OS even after a crash."""
    def __init__(self, path: Path):
        self.path, self.file = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise Fault('DEVICE_BUSY') from exc
        return self

    def __exit__(self, *_):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


def private_url(value: str, *, model: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Fault('INVALID_ENDPOINT')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and address.is_unspecified:
        raise Fault('INVALID_ENDPOINT')
    # No silently insecure Internet model keys or plaintext public Appium.
    if parsed.scheme == 'http' and not (
        parsed.hostname == 'localhost' or (address and (address.is_private or address.is_loopback))
    ):
        raise Fault('HTTPS_REQUIRED')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Fault('REDIRECT_REFUSED')


class Http:
    def __init__(self, base: str, *, token: str = '', model: bool = False):
        self.base = private_url(base, model=model)
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, path: str, body=None, *, timeout=20, mutation=False):
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None if body is None else dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                return loads(raw, MAX_RESPONSE)
        except PermissionError as exc:
            raise Fault('HOST_EXEC_POLICY_DENIED', uncertain=mutation) from exc
        except urllib.error.HTTPError as exc:
            # HTTP rejection is not always proof a device command did not execute.
            raise Fault('UPSTREAM_REJECTED', uncertain=mutation) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise Fault('TRANSPORT_UNAVAILABLE', uncertain=mutation) from exc
        except Fault as exc:
            if mutation:
                exc.uncertain = True
            raise


class Config:
    def __init__(self, path: Path | None = None):
        self.home = Path(os.environ.get('AUTO_PHONE_HOME', str(Path.home() / '.auto-phone-skill'))).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            self.home.chmod(0o700)
        local = Path(__file__).resolve().parent.parent / 'config.json'
        explicit = path is not None or bool(os.environ.get('AUTO_PHONE_CONFIG'))
        self.source = 'explicit' if path is not None else 'environment' if explicit else 'private'
        path = Path(path or os.environ.get('AUTO_PHONE_CONFIG') or self.home / 'config.json').expanduser()
        if explicit and not path.is_file():
            raise Fault('CONFIG_NOT_FOUND')
        if not explicit and not path.exists() and local.is_file():
            path, self.source = local, 'skill_local_compat'
        self.path = path.resolve()
        self.local_config_ignored = local.is_file() and self.path != local.resolve()
        raw = loads(self.path.read_bytes()) if self.path.exists() else {}
        self.raw = loads(dumps(raw))
        if set(raw) - {'devices', 'auto_install_appium', 'confirm_all', 'max_steps', 'llm', 'java_home', 'android_home'}:
            raise Fault('INVALID_CONFIG')
        for field in ('java_home', 'android_home'):
            if field in raw and (not isinstance(raw[field], str) or not raw[field].strip() or len(raw[field]) > 2048):
                raise Fault('INVALID_CONFIG')
        self.auto_install = raw.get('auto_install_appium', True)
        self.confirm_all = raw.get('confirm_all', False)
        self.max_steps = raw.get('max_steps', 40)
        if type(self.auto_install) is not bool or type(self.confirm_all) is not bool or type(self.max_steps) is not int or not 1 <= self.max_steps <= 200:
            raise Fault('INVALID_CONFIG')
        self.devices = raw.get('devices', {})
        if not isinstance(self.devices, dict) or len(self.devices) > 128:
            raise Fault('INVALID_CONFIG')
        if not self.devices and os.environ.get('AUTO_PHONE_UDID'):
            self.devices = {os.environ.get('AUTO_PHONE_DEVICE_ID', 'cloud-1'): {
                'udid': os.environ['AUTO_PHONE_UDID'],
                'appium_url': os.environ.get('AUTO_PHONE_APPIUM_URL', 'http://127.0.0.1:4723'),
            }}
        from .contracts import IDENTIFIER, validate
        targets, ports = set(), set()
        for name, device in self.devices.items():
            validate(IDENTIFIER, name)
            if not isinstance(device, dict) or set(device) - {'udid', 'appium_url', 'system_port', 'auto_start'}:
                raise Fault('INVALID_CONFIG')
            udid = device.get('udid')
            if not isinstance(udid, str) or not 1 <= len(udid) <= 255 or any(ord(c) < 33 for c in udid):
                raise Fault('DEVICE_TARGET_REQUIRED')
            device['appium_url'] = private_url(device.get('appium_url', 'http://127.0.0.1:4723'))
            device.setdefault('system_port', 8200 + len(targets))
            device.setdefault('auto_start', True)
            if type(device['auto_start']) is not bool or type(device['system_port']) is not int or not 1024 <= device['system_port'] <= 65535:
                raise Fault('INVALID_CONFIG')
            target = (device['appium_url'], udid)
            port = (device['appium_url'], device['system_port'])
            if target in targets or port in ports:
                raise Fault('DUPLICATE_DEVICE_MAPPING')
            targets.add(target); ports.add(port)
        self.llm = raw.get('llm', {})
        if not isinstance(self.llm, dict) or set(self.llm) - {'base_url', 'model', 'api_key_env', 'max_output_tokens'}:
            raise Fault('INVALID_CONFIG')
        self.llm.setdefault('base_url', os.environ.get('AUTO_PHONE_LLM_BASE_URL', ''))
        self.llm.setdefault('model', os.environ.get('AUTO_PHONE_LLM_MODEL', ''))
        self.llm.setdefault('api_key_env', 'AUTO_PHONE_LLM_API_KEY')
        self.llm.setdefault('max_output_tokens', 700)
        if any(not isinstance(self.llm[x], str) for x in ('base_url', 'model', 'api_key_env')):
            raise Fault('INVALID_CONFIG')
        if type(self.llm['max_output_tokens']) is not int or not 100 <= self.llm['max_output_tokens'] <= 2000:
            raise Fault('INVALID_CONFIG')
        if self.llm['base_url']:
            private_url(self.llm['base_url'], model=True)

    def device(self, name: str) -> dict:
        if name not in self.devices:
            raise Fault('UNKNOWN_DEVICE')
        return self.devices[name]

    def identity(self, name: str) -> str:
        return hashlib.sha256(dumps(self.device(name)).encode()).hexdigest()

    def lock(self, name: str):
        # Aliases resolving to the same physical endpoint cannot bypass the lock.
        d = self.device(name)
        key = hashlib.sha256((d['appium_url'] + '\0' + d['udid']).encode()).hexdigest()
        return Lock(self.home / ('device-' + key + '.lock'))

    def doctor(self):
        from .environment import inspect
        details = inspect(self)
        return {**details, 'python': sys.version.split()[0], 'configured_devices': sorted(self.devices),
                'node_available': bool(shutil.which('node')), 'adb_available': bool(shutil.which('adb')),
                'java_available': bool(shutil.which('java')), 'transport': 'stdio-or-cli',
                'runtime_dependencies': 0}


def ensure_appium(config: Config, device: dict) -> None:
    """Reuse Appium; otherwise start only a localhost child, never a public service."""
    http = Http(device['appium_url'])
    def healthy():
        try:
            data = http.call('GET', '/status', timeout=2)
            value = data.get('value')
            return isinstance(value, dict) and value.get('ready') is True
        except Fault as exc:
            if exc.code == 'HOST_EXEC_POLICY_DENIED':
                raise
            return False
    if healthy():
        return
    parsed = urllib.parse.urlsplit(device['appium_url'])
    if not device['auto_start'] or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.scheme != 'http' or parsed.path:
        raise Fault('APPIUM_NOT_READY')
    with Lock(config.home / 'appium-start.lock'):
        if healthy():
            return
        executable = shutil.which('appium')
        managed_main = config.home / 'node/node_modules/appium/build/lib/main.js'
        from .environment import require_local, stage
        if not executable and not managed_main.exists() and not config.auto_install:
            raise Fault('APPIUM_INSTALL_REQUIRED')
        env = require_local(config)
        if executable:
            command = [executable]
        elif managed_main.exists() and shutil.which('node'):
            command = [shutil.which('node'), str(managed_main)]
            env['APPIUM_HOME'] = str(config.home / 'appium-home')
        else:
            if not config.auto_install:
                raise Fault('APPIUM_INSTALL_REQUIRED')
            npm, node = shutil.which('npm'), shutil.which('node')
            if not npm or not node:
                raise Fault('NODE_NPM_REQUIRED')
            # Android SDK/Java are host toolchains, not silently installed with root access.
            env['APPIUM_HOME'] = str(config.home / 'appium-home')
            try:
                stage(config, 'install_appium')
                subprocess.run([npm, 'install', '--prefix', str(config.home / 'node'),
                                '--no-audit', '--no-fund', 'appium@3'],
                               check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                command = [node, str(managed_main)]
                subprocess.run([*command, 'driver', 'install', 'uiautomator2'],
                               check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            except PermissionError as exc:
                raise Fault('HOST_EXEC_POLICY_DENIED') from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise Fault('APPIUM_INSTALL_FAILED') from exc
        # Drivers stay in a private Appium home; never alter a global driver's installation.
        env['APPIUM_HOME'] = str(config.home / 'appium-home')
        stage(config, 'check_driver')
        try:
            listing = subprocess.run([*command, 'driver', 'list', '--installed', '--json'],
                                     check=True, capture_output=True, timeout=30, env=env)
            installed = loads(listing.stdout)
            if 'uiautomator2' not in installed:
                if not config.auto_install:
                    raise Fault('UIAUTOMATOR2_INSTALL_REQUIRED')
                subprocess.run([*command, 'driver', 'install', 'uiautomator2'], check=True,
                               timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        except PermissionError as exc:
            raise Fault('HOST_EXEC_POLICY_DENIED') from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise Fault('UIAUTOMATOR2_INSTALL_FAILED') from exc
        address = '::1' if parsed.hostname == '::1' else '127.0.0.1'
        stage(config, 'start_appium')
        log = open(config.home / 'appium.log', 'ab')
        try:
            process = subprocess.Popen([*command, '--address', address, '--port', str(parsed.port or 80),
                                        '--log-level', 'error', '--log-no-colors'],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                       start_new_session=os.name != 'nt',
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        except PermissionError as exc:
            raise Fault('HOST_EXEC_POLICY_DENIED') from exc
        except OSError as exc:
            raise Fault('APPIUM_START_FAILED') from exc
        finally:
            log.close()
        for _ in range(100):
            if healthy():
                return
            if process.poll() is not None:
                raise Fault('APPIUM_START_FAILED')
            time.sleep(0.2)
        with contextlib.suppress(OSError):
            process.terminate()
        raise Fault('APPIUM_START_TIMEOUT')
