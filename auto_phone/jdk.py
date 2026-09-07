"""Explicit opt-in Temurin JDK setup: host architecture, official metadata and SHA-256."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .contracts import Fault, loads
from .environment import architecture, capture, stage

HOSTS = {'api.adoptium.net', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
MAX_ARCHIVE = 500 * 1024 * 1024


def trusted_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
        raise Fault('UNTRUSTED_JDK_URL')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, path, limit, seconds=180):
    opener = urllib.request.build_opener(Redirect())
    request = urllib.request.Request(trusted_url(url), headers={'User-Agent': 'auto-phone-skill/0.3.1'})
    started, size = time.monotonic(), 0
    try:
        with opener.open(request, timeout=20) as response, path.open('wb') as out:
            while True:
                block = response.read(65536)
                if not block:
                    break
                size += len(block)
                if size > limit or time.monotonic() - started > seconds:
                    raise Fault('JDK_DOWNLOAD_LIMIT')
                out.write(block)
    except urllib.error.HTTPError as exc:
        raise Fault('JDK_DOWNLOAD_DENIED' if exc.code in (401, 403) else 'JDK_DOWNLOAD_FAILED') from exc
    except PermissionError as exc:
        raise Fault('HOST_EXEC_POLICY_DENIED') from exc
    except (OSError, urllib.error.URLError) as exc:
        raise Fault('JDK_DOWNLOAD_FAILED') from exc


def select_asset(raw, arch, os_name):
    items = loads(b'{"items":' + raw + b'}', 1048576)['items']
    if not isinstance(items, list):
        raise Fault('INVALID_JDK_METADATA')
    for item in items:
        binary = item.get('binary', {}) if isinstance(item, dict) else {}
        if (binary.get('architecture'), binary.get('os'), binary.get('image_type'), binary.get('jvm_impl')) != (arch, os_name, 'jdk', 'hotspot'):
            continue
        pkg = binary.get('package', {})
        digest, link, size = pkg.get('checksum'), pkg.get('link'), pkg.get('size')
        if not isinstance(digest, str) or not re.fullmatch('[a-fA-F0-9]{64}', digest):
            raise Fault('INVALID_JDK_METADATA')
        if not isinstance(link, str) or type(size) is not int or not 1 <= size <= MAX_ARCHIVE:
            raise Fault('INVALID_JDK_METADATA')
        trusted_url(link)
        if not link.startswith('https://github.com/adoptium/temurin17-binaries/releases/download/'):
            raise Fault('UNTRUSTED_JDK_URL')
        return link, digest.lower(), size
    raise Fault('JDK_PLATFORM_UNSUPPORTED')


def unpack(archive, root):
    """No device files, path escapes, duplicate names or unchecked archive links."""
    root.mkdir()
    seen, total, links = set(), 0, []
    def target(name, size):
        nonlocal total
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or p.as_posix() in seen:
            raise Fault('INVALID_JDK_ARCHIVE')
        total += size
        seen.add(p.as_posix())
        if len(seen) > 50000 or total > 2 * 1024**3:
            raise Fault('INVALID_JDK_ARCHIVE')
        dst = root.joinpath(*p.parts)
        if not dst.resolve().is_relative_to(root.resolve()):
            raise Fault('INVALID_JDK_ARCHIVE')
        dst.parent.mkdir(parents=True, exist_ok=True)
        return dst
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                dst = target(entry.filename, entry.file_size)
                mode = entry.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise Fault('INVALID_JDK_ARCHIVE')
                if entry.is_dir():
                    dst.mkdir(exist_ok=True)
                else:
                    with z.open(entry) as src, dst.open('wb') as out:
                        shutil.copyfileobj(src, out)
                    dst.chmod(0o700 if mode & 0o111 else 0o600)
    else:
        with tarfile.open(archive, 'r:*') as tar:
            for entry in tar:
                dst = target(entry.name, entry.size)
                if entry.isdir():
                    dst.mkdir(exist_ok=True)
                elif entry.isfile():
                    with tar.extractfile(entry) as src, dst.open('wb') as out:
                        shutil.copyfileobj(src, out)
                    dst.chmod(0o700 if entry.mode & 0o111 else 0o600)
                elif entry.issym() or entry.islnk():
                    links.append((dst, entry.linkname, entry.issym()))
                else:
                    raise Fault('INVALID_JDK_ARCHIVE')
        for dst, name, symbolic in links:
            if Path(name).is_absolute() or '\\' in name or ':' in name:
                raise Fault('INVALID_JDK_ARCHIVE')
            source = (dst.parent / name if symbolic else root / name).resolve()
            if not source.is_relative_to(root.resolve()) or not source.exists():
                raise Fault('INVALID_JDK_ARCHIVE')
            if symbolic:
                dst.symlink_to(name)
            else:
                os.link(source, dst)


def install_jdk(config):
    from .platform import Lock
    arch = architecture()
    os_name = {'Linux': 'linux', 'Darwin': 'mac', 'Windows': 'windows'}.get(platform.system())
    if arch == 'unsupported' or not os_name or platform.libc_ver()[0] == 'musl':
        raise Fault('JDK_PLATFORM_UNSUPPORTED')
    with Lock(config.home / 'jdk-install.lock'):
        stage(config, 'jdk_metadata')
        base = config.home / 'toolchains'
        base.mkdir(exist_ok=True)
        destination = base / 'jdk'
        if destination.exists():
            ext = '.exe' if os.name == 'nt' else ''
            props = capture([str(destination / ('bin/java' + ext)), '-XshowSettings:properties', '-version'], dict(os.environ))
            actual = re.search(r'os.arch\s*=\s*(\S+)', props)
            if (actual and architecture(actual[1]) == arch and
                    all((destination / ('bin/' + n + ext)).is_file() for n in ('java', 'javac', 'keytool')) and
                    capture([str(destination / ('bin/javac' + ext)), '-version'], dict(os.environ))):
                stage(config, 'jdk_installed', 'OK')
                return
            raise Fault('JDK_ALREADY_PRESENT_VALIDATE_JAVA_HOME')
        with tempfile.TemporaryDirectory(prefix='jdk-', dir=base) as tmp:
            temp = Path(tmp)
            query = urllib.parse.urlencode({'architecture': arch, 'image_type': 'jdk', 'os': os_name, 'vendor': 'eclipse'})
            meta = temp / 'assets.json'
            download('https://api.adoptium.net/v3/assets/latest/17/hotspot?' + query, meta, 1048576, 30)
            url, expected, size = select_asset(meta.read_bytes(), arch, os_name)
            stage(config, 'jdk_download')
            archive = temp / 'jdk.archive'
            download(url, archive, size)
            with archive.open('rb') as handle:
                checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
            if archive.stat().st_size != size or checksum != expected:
                raise Fault('JDK_CHECKSUM_MISMATCH')
            stage(config, 'jdk_verify')
            unpack(archive, temp / 'unpacked')
            ext = '.exe' if os.name == 'nt' else ''
            homes = [p.parent.parent for p in (temp / 'unpacked').rglob('javac' + ext) if p.parent.name == 'bin']
            if len(homes) != 1 or not all((homes[0] / ('bin/' + n + ext)).is_file() for n in ('java', 'javac', 'keytool')):
                raise Fault('INVALID_JDK_ARCHIVE')
            env = dict(os.environ)
            props = capture([str(homes[0] / ('bin/java' + ext)), '-XshowSettings:properties', '-version'], env)
            actual = re.search(r'os.arch\s*=\s*(\S+)', props)
            if not actual or architecture(actual[1]) != arch or not capture([str(homes[0] / ('bin/javac' + ext)), '-version'], env):
                raise Fault('JDK_ARCHITECTURE_OR_RUNTIME_MISMATCH')
            homes[0].replace(destination)
        stage(config, 'jdk_installed', 'OK')
