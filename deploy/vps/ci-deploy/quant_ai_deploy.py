#!/usr/bin/python3 -I
"""Root forced command for the Quant service only; no shell or online install."""
from __future__ import annotations

import fcntl
from contextlib import closing
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path


RELEASE_ID = r'[0-9]{8}-quant-v[0-9]+(?:\.[0-9]+)*-[0-9a-f]{7,40}(?:-r[0-9]+)?'
DEPLOY_COMMAND = re.compile(rf'deploy ({RELEASE_ID}) ([0-9a-f]{{64}})')
ROOT_CONFIG = Path('/etc/ai-services/quant-deploy.json')
SERVICE = 'quant-ai.service'
SERVICE_USER = 'quant-ai'
DISCOVERY = '/.well-known/oauth-protected-resource'
MCP = '/mcp'
SAFE_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}


class DeployError(Exception):
    """Only fixed, non-sensitive error codes may enter a receipt."""


@dataclass(frozen=True)
class Settings:
    releases_dir: Path = Path('/opt/ai-services/releases')
    config_path: Path = Path('/etc/ai-services/quant.json')
    dropin_path: Path = Path('/etc/systemd/system/quant-ai.service.d/50-approved-runtime.conf')
    lock_path: Path = Path('/run/lock/quant-ai-deploy.lock')
    python: Path = Path('/usr/bin/python3')
    db_path: Path = Path('/var/lib/quant-ai/tasks.sqlite')
    config_sha256: str = ''
    baseline_release: str = ''
    baseline_source_sha: str = ''
    port: int = 8091

    @classmethod
    def load(cls, path=ROOT_CONFIG):
        if not path.exists() and not path.is_symlink():
            raise DeployError('deploy_config_required')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise DeployError('unsafe_deploy_config')
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - {field.name for field in fields(cls)}:
            raise DeployError('invalid_deploy_config')
        for name in ('releases_dir', 'config_path', 'dropin_path', 'lock_path', 'python', 'db_path'):
            if name in data:
                value = data[name]
                # Paths also enter a systemd unit; reject whitespace and specifiers.
                if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or '..' in Path(value).parts:
                    raise DeployError('invalid_deploy_path')
                data[name] = Path(value)
        result = cls(**data)
        if (not isinstance(result.baseline_release, str)
                or re.fullmatch(RELEASE_ID, result.baseline_release) is None
                or not isinstance(result.config_sha256, str)
                or re.fullmatch(r'[0-9a-f]{64}', result.config_sha256) is None
                or not isinstance(result.baseline_source_sha, str)
                or re.fullmatch(r'[0-9a-f]{40}', result.baseline_source_sha) is None
                or type(result.port) is not int or not 1 <= result.port <= 65535):
            raise DeployError('invalid_deploy_config')
        short_sha = result.baseline_release.split('-')[3]
        if not result.baseline_source_sha.startswith(short_sha):
            raise DeployError('baseline_source_mismatch')
        return result

    @property
    def baseline(self):
        return self.releases_dir / self.baseline_release


def parse_command(value):
    if value == 'status':
        return value, ()
    rollback = re.fullmatch(r'rollback ([0-9a-f]{40})', value)
    if rollback:
        return 'rollback', rollback.groups()
    match = DEPLOY_COMMAND.fullmatch(value)
    if match is None:
        raise DeployError('command_not_allowed')
    return 'deploy', match.groups()


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Runner:
    def run(self, argv, *, timeout=120):
        try:
            result = subprocess.run([str(arg) for arg in argv], check=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=SAFE_ENV, timeout=timeout, text=True)
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise DeployError('command_failed') from None


def wheel_requirement(filename, digest):
    match = re.fullmatch(r'([A-Za-z0-9_.]+)-([A-Za-z0-9_.!+]+)-(?:[0-9][A-Za-z0-9_]*-)?[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl', filename)
    if match is None:
        raise DeployError('invalid_wheel_name')
    return f'{match[1]}=={match[2]} --hash=sha256:{digest}'


class BoundedReader:
    def __init__(self, source, limit):
        self.source, self.remaining = source, limit

    def read(self, size):
        result = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(result)
        if self.remaining < 0:
            raise DeployError('expanded_bundle_too_large')
        return result


class BundleStager:
    MAX_BUNDLE = 256 * 1024 * 1024
    MAX_EXPANDED = 1024 * 1024 * 1024
    MAX_MEMBERS = 4096

    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner

    def stage(self, release_id, expected_hash, source):
        if re.fullmatch(RELEASE_ID, release_id) is None:
            raise DeployError('invalid_release_id')
        target = self.settings.releases_dir / release_id
        if target.exists() or target.is_symlink():
            raise DeployError('release_already_exists')
        self.settings.releases_dir.mkdir(parents=True, exist_ok=True)
        # Failed releases remain reserved for diagnosis; never reuse a release ID.
        with tempfile.TemporaryDirectory(prefix='.incoming-', dir=self.settings.releases_dir) as temporary:
            root = Path(temporary)
            bundle = root / 'bundle.tar.gz'
            digest, length = hashlib.sha256(), 0
            with bundle.open('xb') as output:
                while True:
                    chunk = source.read(min(1024 * 1024, self.MAX_BUNDLE - length + 1))
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > self.MAX_BUNDLE:
                        raise DeployError('bundle_too_large')
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected_hash:
                raise DeployError('bundle_hash_mismatch')
            unpacked = root / 'release'
            unpacked.mkdir(mode=0o755)
            self.extract(bundle, unpacked)
            self.verify(unpacked, release_id)
            # mkdir, rather than rename-over-existing, reserves a fresh destination.
            target.mkdir(mode=0o755)
            for child in unpacked.iterdir():
                child.rename(target / child.name)
        self.runner.run([self.settings.python, '-I', '-S', '-m', 'venv', target / 'venv'])
        self.runner.run([target / 'venv/bin/python', '-I', '-m', 'pip', '--isolated', 'install',
            '--no-index', '--no-deps', '--only-binary=:all:', '--require-hashes', '--no-cache-dir',
            '--find-links', target / 'wheels', '-r', target / 'requirements.txt'])
        self.runner.run([target / 'venv/bin/python', '-I', '-m', 'pip', '--isolated', 'check'])
        # Root owns the tree; service and deploy users cannot alter a release.
        for directory, directories, filenames in os.walk(target):
            os.chmod(directory, 0o755)
            for name in filenames:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, 0o755 if path.stat().st_mode & 0o111 else 0o644)
        return target

    def extract(self, bundle, destination):
        names, total = set(), 0
        try:
            # Bound all decompressed bytes, including hidden tar metadata/padding.
            # The per-file budget below separately bounds actual payload data.
            with gzip.open(bundle, 'rb') as compressed, tarfile.open(fileobj=BoundedReader(
                    compressed, self.MAX_EXPANDED + self.MAX_MEMBERS * 1024), mode='r|') as archive:
                for member in archive:
                    name = member.name
                    if name in names or len(names) >= self.MAX_MEMBERS:
                        raise DeployError('invalid_bundle_members')
                    names.add(name)
                    if name == 'wheels' and member.isdir():
                        (destination / 'wheels').mkdir(exist_ok=True, mode=0o755)
                        continue
                    allowed = name in ('requirements.txt', 'SHA256SUMS') or re.fullmatch(r'wheels/[A-Za-z0-9_.!+-]+\.whl', name)
                    if not member.isfile() or not allowed or member.size < 0:
                        raise DeployError('unsafe_bundle_member')
                    total += member.size
                    if total > self.MAX_EXPANDED:
                        raise DeployError('expanded_bundle_too_large')
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with archive.extractfile(member) as incoming, path.open('xb') as output:
                        while chunk := incoming.read(1024 * 1024):
                            output.write(chunk)
                    path.chmod(0o644)
        except (tarfile.TarError, EOFError):
            raise DeployError('invalid_bundle_archive') from None

    def verify(self, root, release_id=None):
        actual = {path.relative_to(root).as_posix(): path for path in root.rglob('*') if path.is_file()}
        if not {'requirements.txt', 'SHA256SUMS'} <= actual.keys():
            raise DeployError('bundle_manifest_missing')
        manifest = {}
        for line in actual['SHA256SUMS'].read_text().splitlines():
            match = re.fullmatch(r'([0-9a-f]{64})  (requirements\.txt|wheels/[A-Za-z0-9_.!+-]+\.whl)', line)
            if match is None or match[2] in manifest:
                raise DeployError('invalid_bundle_manifest')
            manifest[match[2]] = match[1]
        if set(manifest) != set(actual) - {'SHA256SUMS'}:
            raise DeployError('bundle_manifest_coverage')
        for name, digest in manifest.items():
            with actual[name].open('rb') as incoming:
                check = hashlib.sha256()
                while chunk := incoming.read(1024 * 1024):
                    check.update(chunk)
            if check.hexdigest() != digest:
                raise DeployError('file_hash_mismatch')
        expected = [wheel_requirement(Path(name).name, digest) for name, digest in manifest.items() if name.startswith('wheels/')]
        packages = [line.split('==')[0].lower().replace('_', '-').replace('.', '-') for line in expected]
        requirements = actual['requirements.txt'].read_text().splitlines()
        if (not expected or len(packages) != len(set(packages)) or len(requirements) != len(expected)
                or set(requirements) != set(expected)
                or 'quant-ai-service' not in packages or 'personal-ai-service' in packages):
            raise DeployError('invalid_pinned_requirements')
        if release_id is not None:
            version = next(line.split('==', 1)[1].split()[0] for line, package in zip(expected, packages)
                           if package == 'quant-ai-service')
            if re.fullmatch(rf'[0-9]{{8}}-quant-v{re.escape(version)}-[0-9a-f]{{7,40}}(?:-r[0-9]+)?', release_id) is None:
                raise DeployError('release_version_mismatch')


class Preflight:
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner

    def check(self, release):
        self.config_gate()
        self.runner.run(['/usr/sbin/runuser', '-u', SERVICE_USER, '--',
            release / 'venv/bin/quant-ai-service', '--check-config', '--config', self.settings.config_path])

    def config_gate(self):
        if hashlib.sha256(self.settings.config_path.read_bytes()).hexdigest() != self.settings.config_sha256:
            raise DeployError('config_drift')

    def drain(self):
        # Never initialize the runtime: its constructors can write to the DB.
        uri = self.settings.db_path.as_uri() + '?mode=ro'
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                counts = [
                    db.execute("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('completed','failed','invalid_output','cancelled')").fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM subscriptions WHERE active=1').fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM event_deliveries WHERE status='pending'").fetchone()[0],
                ]
                if any(counts):
                    raise DeployError('inflight_work')
        except sqlite3.Error:
            raise DeployError('drain_unknown') from None


class Switcher:
    def __init__(self, settings, runner):
        self.settings, self.runner = settings, runner

    def render(self, release):
        return (f'[Service]\nExecStart=\nExecStart={release}/venv/bin/quant-ai-service '
                f'--config {self.settings.config_path} --port {self.settings.port}\n').encode()

    def current(self):
        text = self.settings.dropin_path.read_text()
        starts = [line[len('ExecStart='):] for line in text.splitlines() if line.startswith('ExecStart=') and line != 'ExecStart=']
        if len(starts) != 1:
            raise DeployError('unrecognized_dropin')
        match = re.fullmatch(r'(/[A-Za-z0-9_./-]+)/venv/bin/quant-ai-service --config /[A-Za-z0-9_./-]+ --port [0-9]+', starts[0])
        if match is None:
            raise DeployError('unrecognized_dropin')
        release = Path(match[1])
        if release.parent != self.settings.releases_dir or re.fullmatch(RELEASE_ID, release.name) is None:
            raise DeployError('unrecognized_release')
        return release

    def backup(self, release_id):
        path = self.settings.dropin_path.with_name(self.settings.dropin_path.name + '.backup-' + release_id)
        with path.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(self.settings.dropin_path.read_bytes())
            output.flush()
            os.fsync(output.fileno())

    def activate(self, release):
        if not (release / 'venv/bin/quant-ai-service').is_file():
            raise DeployError('release_executable_missing')
        atomic_write(self.settings.dropin_path, self.render(release))
        self.runner.run(['/usr/bin/systemctl', 'daemon-reload'])
        self.runner.run(['/usr/bin/systemctl', 'restart', SERVICE])

    def stop(self):
        self.runner.run(['/usr/bin/systemctl', 'stop', SERVICE])

    def resume(self):
        self.runner.run(['/usr/bin/systemctl', 'start', SERVICE])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HealthProbe:
    def __init__(self, settings, runner, *, request=None, clock=time.monotonic, sleep=time.sleep):
        self.settings, self.runner = settings, runner
        self.request = request or self.http_status
        self.clock, self.sleep = clock, sleep

    @staticmethod
    def http_status(url, method):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, method=method, data=b'{}' if method == 'POST' else None,
                                         headers={'Content-Type': 'application/json'})
        try:
            with opener.open(request, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def unit(self):
        output = self.runner.run(['/usr/bin/systemctl', 'show', SERVICE,
            '--property=ActiveState', '--property=SubState', '--property=ExecStart'], timeout=5)
        properties = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        match = re.search(r'(?:^|[ ;{])path=([^ ;}]+)', properties.get('ExecStart', ''))
        release = None
        if match:
            executable = Path(match[1])
            candidate = executable.parent.parent.parent
            if (executable == candidate / 'venv/bin/quant-ai-service'
                    and candidate.parent == self.settings.releases_dir
                    and re.fullmatch(RELEASE_ID, candidate.name)):
                release = candidate
        running = properties.get('ActiveState') == 'active' and properties.get('SubState') == 'running'
        return running, release

    def check(self, expected):
        try:
            running, release = self.unit()
            origin = f'http://127.0.0.1:{self.settings.port}'
            discovery = self.request(origin + DISCOVERY, 'GET')
            mcp = self.request(origin + MCP, 'POST')
            healthy = running and release is not None and release == expected and discovery == 200 and mcp == 401
            return {'healthy': healthy, 'active_release_dir': str(release) if release else None,
                    'unit_running': running, 'discovery_status': discovery, 'mcp_status': mcp}
        except (DeployError, OSError, ValueError):
            return {'healthy': False, 'active_release_dir': None}

    def wait(self, release, *, seconds=45):
        deadline = self.clock() + seconds
        while True:
            result = self.check(release)
            if result['healthy'] or self.clock() >= deadline:
                return result
            self.sleep(min(2, max(0, deadline - self.clock())))

    def version(self, release):
        if release is None:
            return None
        try:
            output = self.runner.run(['/usr/sbin/runuser', '-u', SERVICE_USER, '--',
                release / 'venv/bin/python', '-I', '-c',
                'from importlib.metadata import version; print(version("quant-ai-service"))'], timeout=5).strip()
            return output if re.fullmatch(r'[A-Za-z0-9.+!-]{1,80}', output) else None
        except DeployError:
            return None


class Deployer:
    def __init__(self, settings, stager, preflight, switcher, health):
        self.settings, self.stager, self.preflight = settings, stager, preflight
        self.switcher, self.health = switcher, health

    def execute(self, command, args=(), source=None):
        receipt = {'command': command, 'service': SERVICE, 'ok': False, 'rolled_back': False,
                   'baseline_source_sha': self.settings.baseline_source_sha}
        try:
            if command == 'status':
                self.preflight.config_gate()
                _, release = self.health.unit()
                receipt.update(health=self.health.check(release), version=self.health.version(release))
                receipt['ok'] = receipt['health']['healthy']
            elif command == 'rollback':
                if args != (self.settings.baseline_source_sha,):
                    raise DeployError('rollback_target_mismatch')
                self.preflight.config_gate()
                self.preflight.drain()
                self.switcher.stop()
                try:
                    self.preflight.config_gate()
                    self.preflight.drain()
                except Exception:
                    self.preflight.config_gate()
                    self.switcher.resume()
                    raise
                self.switcher.activate(self.settings.baseline)
                receipt.update(health=self.health.wait(self.settings.baseline),
                               version=self.health.version(self.settings.baseline))
                receipt['rolled_back'] = True
                receipt['ok'] = receipt['health']['healthy']
            else:
                release_id, bundle_hash = args
                receipt['requested_release'] = release_id
                receipt['phase'] = 'stage'
                release = self.stager.stage(release_id, bundle_hash, source)
                receipt['phase'] = 'preflight'
                self.preflight.check(release)
                receipt['phase'] = 'backup'
                self.switcher.backup(release_id)
                self.preflight.drain()
                self.switcher.stop()
                try:
                    self.preflight.config_gate()
                    self.preflight.drain()
                except Exception:
                    self.preflight.config_gate()
                    self.switcher.resume()
                    raise
                try:
                    receipt['phase'] = 'switch'
                    self.switcher.activate(release)
                    receipt['phase'] = 'health'
                    receipt['health'] = self.health.wait(release)
                    if not receipt['health']['healthy']:
                        raise DeployError('health_check_failed')
                    receipt.update(ok=True, phase='complete', version=self.health.version(release))
                except Exception:
                    receipt['error'] = 'activation_or_health_failed'
                    receipt['rollback_attempted'] = True
                    receipt['phase'] = 'rollback'
                    try:
                        self.switcher.stop()
                        self.preflight.config_gate()
                        self.preflight.drain()
                        self.switcher.activate(self.settings.baseline)
                        receipt['health'] = self.health.wait(self.settings.baseline)
                        receipt['rolled_back'] = True
                        receipt['version'] = self.health.version(self.settings.baseline)
                        if not receipt['health']['healthy']:
                            receipt['error'] = 'rollback_health_failed'
                    except Exception:
                        receipt['error'] = 'rollback_failed'
        except Exception:
            # Subprocess output and configuration values never enter receipts.
            receipt['error'] = 'command_failed_before_completion'
        return receipt


def main():
    command = 'rejected'
    receipt = {'command': command, 'ok': False, 'rolled_back': False}
    descriptor = None
    try:
        command, args = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        receipt['command'] = command
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise DeployError('root_forced_command_required')
        settings = Settings.load()
        descriptor = os.open(settings.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('deployment_busy') from None
        runner = Runner()
        deployer = Deployer(settings, BundleStager(settings, runner), Preflight(settings, runner),
                            Switcher(settings, runner), HealthProbe(settings, runner))
        receipt = deployer.execute(command, args, sys.stdin.buffer)
    except DeployError as error:
        receipt['error'] = str(error)
    except Exception:
        receipt['error'] = 'command_failed'
    finally:
        if descriptor is not None:
            os.close(descriptor)
    print(json.dumps(receipt, separators=(',', ':'), sort_keys=True))
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
