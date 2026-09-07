"""Bounded preflight and private setup. Never enumerate/select phones or edit host policy."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time

from .contracts import Fault, dumps, loads

HINTS = {
    'UNKNOWN_DEVICE': '由宿主用 setup 登记已分配的真实设备；不要猜测序列号或搜索整台机器。',
    'CONFIG_NOT_FOUND': '显式 --config / AUTO_PHONE_CONFIG 文件不存在；修正该路径后重试。',
    'JDK_REQUIRED': '需要 JDK，而非仅有 java 的 JRE。可显式 setup install_jdk=true 在用户目录安装匹配架构的 JDK。',
    'ANDROID_SDK_REQUIRED': '提供真实 SDK 根目录 android_home / ANDROID_HOME；需含可运行的 platform-tools/adb。不会伪造 SDK 或下载 x64 工具到 ARM。',
    'NODE_NPM_REQUIRED': '本地 Appium 需要 Node/npm，或由宿主提供已有 Appium；不安装系统工具链。',
    'NODE_VERSION_UNSUPPORTED': 'Appium 3 需要 Node ^20.19 / ^22.12 / >=24 和 npm >=10。',
    'APPIUM_NOT_READY': '配置的 Appium 未就绪。用 prepare 查看单次初始化结果；不要重复创建任务。',
    'HOST_EXEC_POLICY_DENIED': '宿主拒绝执行。停止并由管理员授权；不要换 curl、Python 或其他工具绕过策略。',
    'INITIALIZATION_FAILED': '初始化未完成，没有下发用户动作。可以修复环境后使用同一幂等键重试。',
    'DEVICE_HAS_ACTIVE_TASK': '用 tasks 查看现有任务。只有无观察、无动作的初始化残留可自动释放，未知动作必须人工核对。',
    'JDK_DOWNLOAD_DENIED': '下载被拒绝；不要更换工具绕过访问限制。使用管理员提供的已验证 JDK 路径。',
    'JDK_DOWNLOAD_FAILED': 'JDK 官方下载未完成；未安装或执行未经校验的内容。可在网络获准后重新 setup。',
}


def hint(code):
    return HINTS.get(code, '保留错误码并运行 doctor。不要重复执行结果未知的动作，也不要读取整套源码排错。')


def write_private(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(dumps(payload) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def stage(config, name, code='RUNNING'):
    write_private(config.home / 'setup-status.json',
                  {'stage': name, 'code': code, 'updated_at': time.time()})


def setup_status(config):
    path = config.home / 'setup-status.json'
    if not path.exists():
        return {'stage': 'not_started', 'code': 'NOT_STARTED', 'updated_at': 0.0}
    data = loads(path.read_bytes(), 4096)
    if set(data) != {'stage', 'code', 'updated_at'}:
        raise Fault('INVALID_SETUP_STATUS')
    return data


def architecture(machine=None):
    value = (machine or platform.machine()).lower()
    table = {'aarch64': 'aarch64', 'arm64': 'aarch64', 'x86_64': 'x64', 'amd64': 'x64'}
    return table.get(value, 'unsupported')


def capture(args, env, timeout=4):
    # Fixed executable/arguments only; no shell, no command strings from UI/LLM.
    try:
        with tempfile.TemporaryFile() as output:
            proc = subprocess.run(args, env=env, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=output, timeout=timeout)
            output.seek(0)
            text = output.read(8192).decode('utf-8', errors='replace')
        return text if proc.returncode == 0 else ''
    except PermissionError as exc:
        raise Fault('HOST_EXEC_POLICY_DENIED') from exc
    except (OSError, subprocess.SubprocessError):
        return ''


def runtime_env(config):
    """Use explicit homes, private verified JDK, or bounded known locations. No global find."""
    env = dict(os.environ)
    suffix = '.exe' if os.name == 'nt' else ''
    java_home = config.raw.get('java_home') or env.get('JAVA_HOME')
    managed = config.home / 'toolchains/jdk'
    if not java_home and (managed / ('bin/javac' + suffix)).is_file():
        java_home = str(managed)
    if not java_home:
        executable = shutil.which('javac')
        if executable:
            java_home = str(Path(executable).resolve().parent.parent)
    if java_home:
        java_home = str(Path(java_home).expanduser().resolve())
        env['JAVA_HOME'] = java_home
        env['PATH'] = str(Path(java_home) / 'bin') + os.pathsep + env.get('PATH', '')
    sdk = config.raw.get('android_home') or env.get('ANDROID_HOME') or env.get('ANDROID_SDK_ROOT')
    if not sdk:
        adb = shutil.which('adb')
        candidates = [Path.home() / 'Android/Sdk', Path.home() / 'Android/sdk',
                      Path('/usr/lib/android-sdk'), Path('/opt/android-sdk')]
        if adb:
            real = Path(adb).resolve()
            if real.parent.name == 'platform-tools':
                candidates.insert(0, real.parent.parent)
        sdk = next((str(p) for p in candidates if (p / ('platform-tools/adb' + suffix)).is_file()), None)
    if sdk:
        sdk = str(Path(sdk).expanduser().resolve())
        env['ANDROID_HOME'] = env['ANDROID_SDK_ROOT'] = sdk
        env['PATH'] = str(Path(sdk) / 'platform-tools') + os.pathsep + env.get('PATH', '')
    return env


def version(text):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', text)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def inspect(config):
    env = runtime_env(config)
    node, npm = shutil.which('node'), shutil.which('npm')
    node_v = version(capture([node, '--version'], env)) if node else (0, 0, 0)
    npm_v = version(capture([npm, '--version'], env)) if npm else (0, 0, 0)
    node_ok = ((node_v[0] == 20 and node_v >= (20, 19, 0)) or
               (node_v[0] == 22 and node_v >= (22, 12, 0)) or node_v[0] >= 24) and npm_v[0] >= 10
    ext = '.exe' if os.name == 'nt' else ''
    home = Path(env['JAVA_HOME']) if env.get('JAVA_HOME') else None
    binaries = [home / ('bin/' + name + ext) for name in ('java', 'javac', 'keytool')] if home else []
    jdk_ok = bool(binaries) and all(p.is_file() for p in binaries)
    if jdk_ok:
        java = capture([str(binaries[0]), '-XshowSettings:properties', '-version'], env)
        javac = capture([str(binaries[1]), '-version'], env)
        arch_match = re.search(r'os.arch\s*=\s*(\S+)', java)
        jdk_ok = bool(java and javac) and (not arch_match or architecture(arch_match[1]) == architecture())
    adb = Path(env['ANDROID_HOME']) / ('platform-tools/adb' + ext) if env.get('ANDROID_HOME') else None
    sdk_ok = bool(adb and adb.is_file() and capture([str(adb), 'version'], env))
    issues = []
    if not config.devices:
        issues.append('UNKNOWN_DEVICE')
    if not node or not npm:
        issues.append('NODE_NPM_REQUIRED')
    elif not node_ok:
        issues.append('NODE_VERSION_UNSUPPORTED')
    if not jdk_ok:
        issues.append('JDK_REQUIRED')
    if not sdk_ok:
        issues.append('ANDROID_SDK_REQUIRED')
    return {'os': platform.system().lower(), 'architecture': architecture(),
            'node_version': '.'.join(map(str, node_v)), 'npm_version': '.'.join(map(str, npm_v)),
            'jdk_ready': bool(jdk_ok), 'sdk_ready': bool(sdk_ok), 'local_prerequisites_ready': not issues,
            'issues': issues, 'config_source': config.source,
            'config_path': str(config.path), 'local_config_ignored': config.local_config_ignored}


def require_local(config):
    report = inspect(config)
    # Mapping has already been resolved by the caller; report all missing local requirements.
    issues = [x for x in report['issues'] if x != 'UNKNOWN_DEVICE']
    if issues:
        raise Fault(issues[0])
    return runtime_env(config)


def prepare(config, device_id):
    from .platform import ensure_appium
    stage(config, 'preflight')
    try:
        ensure_appium(config, config.device(device_id))
    except Fault as exc:
        stage(config, 'blocked', exc.code)
        raise
    except Exception as exc:
        stage(config, 'blocked', 'SETUP_FAILED')
        raise Fault('SETUP_FAILED') from exc
    stage(config, 'appium_ready', 'OK')


def configure(config, body):
    """Host-controlled device assignment only. This command is not exposed as an MCP tool."""
    from .contracts import IDENTIFIER, obj, string, integer, validate
    from .platform import Config, Lock, private_url
    schema = obj({'device_id': IDENTIFIER, 'udid': string(255, minimum=1),
                  'appium_url': string(2048, minimum=1), 'system_port': integer(1024, 65535),
                  'java_home': string(2048, minimum=1), 'android_home': string(2048, minimum=1),
                  'replace_device': {'type': 'boolean'}, 'install_jdk': {'type': 'boolean'}}, ['device_id'])
    validate(schema, body)
    with Lock(config.home / 'configure.lock'):
        # Reload under lock; never lose another process's device registration.
        original_source = config.source
        config = Config(config.path) if config.path.exists() else config
        raw = loads(dumps(config.raw))
        devices = raw.setdefault('devices', {})
        if not devices:
            devices.update(loads(dumps(config.devices)))
        old = devices.get(body['device_id'])
        d = dict(old or {})
        for key in ('udid', 'appium_url', 'system_port'):
            if key in body:
                d[key] = body[key]
        if not d.get('udid'):
            raise Fault('DEVICE_TARGET_REQUIRED')
        if old and any(old.get(k) != d.get(k) for k in ('udid', 'appium_url')) and not body.get('replace_device'):
            raise Fault('DEVICE_REPLACEMENT_REQUIRES_APPROVAL')
        d['appium_url'] = private_url(d.get('appium_url', 'http://127.0.0.1:4723'))
        if 'system_port' not in d:
            used = {x.get('system_port') for x in devices.values()}
            d['system_port'] = next(n for n in range(8200, 9000) if n not in used)
        devices[body['device_id']] = d
        for key in ('java_home', 'android_home'):
            if key in body:
                raw[key] = str(Path(body[key]).expanduser().resolve())
        fd, name = tempfile.mkstemp(prefix='validate-', suffix='.json', dir=config.home)
        os.close(fd)
        trial = Path(name)
        try:
            write_private(trial, raw)
            Config(trial)  # Full semantic validation before replacing private configuration.
        finally:
            trial.unlink(missing_ok=True)
        destination = config.path if original_source in {'explicit', 'environment'} else config.home / 'config.json'
        if destination.exists():
            backup = config.home / 'config-backups' / (str(time.time_ns()) + '.json')
            write_private(backup, loads(destination.read_bytes()))
        write_private(destination, raw)
    return Config(destination)
