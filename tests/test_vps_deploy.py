"""Local fakes only: no real systemd, VPS, SSH, provider or root mutation."""
import contextlib
from dataclasses import replace
import fcntl
import hashlib
import io
import json
from pathlib import Path
import stat
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'deploy/vps/ci-deploy'
sys.path.insert(0, str(SCRIPTS))
import quant_ai_deploy as deploy  # noqa: E402
from ci_bundle import bundle  # noqa: E402
from ci_release import verify  # noqa: E402
from ci_client import external_check, public_origin, validate_receipt  # noqa: E402

RELEASE = '20261015-quant-v2.0.7-abcdef123456-r12'
BASELINE = '20261014-quant-v2.0.7-abcdef1'
BASELINE_SHA = 'abcdef1' + '0' * 33


class FakeRunner:
    def __init__(self, output=''):
        self.calls = []
        self.output = output

    def run(self, argv, **kwargs):
        self.calls.append([str(value) for value in argv])
        return self.output


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = replace(deploy.Settings(), baseline_release=BASELINE, baseline_source_sha=BASELINE_SHA,
            config_sha256=hashlib.sha256(b'{}').hexdigest(), db_path=self.root / 'tasks.sqlite', releases_dir=self.root / 'releases',
            config_path=self.root / 'quant.json', dropin_path=self.root / 'systemd/50-approved-runtime.conf',
            lock_path=self.root / 'deploy.lock')
        self.settings.config_path.write_bytes(b'{}')
        self.runner = FakeRunner()

    def archive(self, members):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, content, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = len(content) if kind == tarfile.REGTYPE else 0
                info.linkname = '../../outside' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                archive.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
        return output.getvalue()

    def valid_bundle(self):
        root = self.root / 'wheelhouse'
        (root / 'wheels').mkdir(parents=True)
        (root / 'wheels/quant_ai_service-2.0.7-py3-none-any.whl').write_bytes(b'synthetic-wheel')
        (root / 'wheels/example_dep-1.2.3-py3-none-any.whl').write_bytes(b'synthetic-dependency')
        output = self.root / 'bundle.tar.gz'
        digest = bundle(root, output)
        return output.read_bytes(), digest, root

    def fake_deployer(self):
        stager, preflight, switcher, health = Mock(), Mock(), Mock(), Mock()
        release = self.settings.releases_dir / RELEASE
        stager.stage.return_value = release
        health.version.return_value = '2.0.7'
        health.wait.return_value = {'healthy': True, 'active_release_dir': str(release)}
        return deploy.Deployer(self.settings, stager, preflight, switcher, health)

    def test_strict_forced_command_allowlist(self):
        digest = 'a' * 64
        self.assertEqual(deploy.parse_command(f'deploy {RELEASE} {digest}'), ('deploy', (RELEASE, digest)))
        self.assertEqual(deploy.parse_command('status'), ('status', ()))
        self.assertEqual(deploy.parse_command('rollback ' + BASELINE_SHA), ('rollback', (BASELINE_SHA,)))
        for command in ('', ' status', 'status\n', 'rollback now', 'rollback', 'status;id', f'deploy ../escape {digest}',
                        f'deploy {RELEASE} {digest.upper()}', f'deploy {RELEASE} {digest}\n',
                        f'deploy {RELEASE.replace("quant", "personal")} {digest}',
                        f'deploy {RELEASE} {digest} extra'):
            with self.subTest(command=command), self.assertRaises(deploy.DeployError):
                deploy.parse_command(command)

    def test_stage_verifies_bundle_and_installs_offline_at_final_path(self):
        data, digest, root = self.valid_bundle()
        stager = deploy.BundleStager(self.settings, self.runner)
        release = stager.stage(RELEASE, digest, io.BytesIO(data))
        self.assertEqual(release, self.settings.releases_dir / RELEASE)
        self.assertTrue((release / 'SHA256SUMS').is_file())
        self.assertEqual(self.runner.calls[0], ['/usr/bin/python3', '-I', '-S', '-m', 'venv', str(release / 'venv')])
        install = self.runner.calls[1]
        for flag in ('--isolated', '--no-index', '--no-deps', '--only-binary=:all:', '--require-hashes', '--no-cache-dir'):
            self.assertIn(flag, install)
        self.assertEqual(install[-1], str(release / 'requirements.txt'))
        self.assertFalse((release / 'requirements.txt').stat().st_mode & 0o022)
        with self.assertRaisesRegex(deploy.DeployError, 'release_already_exists'):
            stager.stage(RELEASE, digest, io.BytesIO(data))

    def test_hash_and_size_failures_do_not_install_or_create_release(self):
        data, digest, _ = self.valid_bundle()
        stager = deploy.BundleStager(self.settings, self.runner)
        with self.assertRaisesRegex(deploy.DeployError, 'bundle_hash_mismatch'):
            stager.stage(RELEASE, '0' * 64, io.BytesIO(data))
        stager.MAX_BUNDLE = 10
        with self.assertRaisesRegex(deploy.DeployError, 'bundle_too_large'):
            stager.stage(RELEASE, digest, io.BytesIO(data))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.settings.releases_dir / RELEASE).exists())

    def test_unsafe_tar_members_are_rejected(self):
        stager = deploy.BundleStager(self.settings, self.runner)
        for name, kind in (('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('wheels/link.whl', tarfile.SYMTYPE), ('wheels/link.whl', tarfile.LNKTYPE),
                           ('venv/bin/quant-ai-service', tarfile.REGTYPE), ('wheels/device.whl', tarfile.CHRTYPE)):
            data = self.archive([(name, b'payload', kind)])
            with self.subTest(name=name, kind=kind), self.assertRaises(deploy.DeployError):
                stager.stage(RELEASE, hashlib.sha256(data).hexdigest(), io.BytesIO(data))
        duplicate = self.archive([('requirements.txt', b'', tarfile.REGTYPE)] * 2)
        with self.assertRaises(deploy.DeployError):
            stager.stage(RELEASE, hashlib.sha256(duplicate).hexdigest(), io.BytesIO(duplicate))
        self.assertFalse((self.root / 'outside').exists())
        self.assertEqual(self.runner.calls, [])

    def test_expanded_size_and_member_count_are_bounded(self):
        data = self.archive([('requirements.txt', b'X' * 100, tarfile.REGTYPE)])
        stager = deploy.BundleStager(self.settings, self.runner)
        stager.MAX_EXPANDED = 10
        with self.assertRaisesRegex(deploy.DeployError, 'expanded_bundle_too_large'):
            stager.stage(RELEASE, hashlib.sha256(data).hexdigest(), io.BytesIO(data))
        stager.MAX_EXPANDED = deploy.BundleStager.MAX_EXPANDED
        stager.MAX_MEMBERS = 0
        with self.assertRaisesRegex(deploy.DeployError, 'invalid_bundle_members'):
            stager.stage(RELEASE, hashlib.sha256(data).hexdigest(), io.BytesIO(data))

    def test_all_decompressed_bytes_are_bounded_including_metadata(self):
        incoming = deploy.BoundedReader(io.BytesIO(b'x' * 100), 10)
        self.assertEqual(incoming.read(5), b'x' * 5)
        with self.assertRaisesRegex(deploy.DeployError, 'expanded_bundle_too_large'):
            incoming.read(100)

    def test_manifest_must_cover_every_payload_and_every_requirement(self):
        _, _, root = self.valid_bundle()
        stager = deploy.BundleStager(self.settings, self.runner)
        stager.verify(root)
        requirements = root / 'requirements.txt'
        manifest = root / 'SHA256SUMS'
        old_requirements, old_manifest = requirements.read_text(), manifest.read_text()
        (root / 'wheels/unlisted-1-py3-none-any.whl').write_bytes(b'extra')
        with self.assertRaisesRegex(deploy.DeployError, 'bundle_manifest_coverage'):
            stager.verify(root)
        (root / 'wheels/unlisted-1-py3-none-any.whl').unlink()
        requirements.write_text('--index-url https://untrusted.example\n')
        manifest.write_text(old_manifest.replace(hashlib.sha256(old_requirements.encode()).hexdigest(),
                                                hashlib.sha256(requirements.read_bytes()).hexdigest()))
        with self.assertRaisesRegex(deploy.DeployError, 'invalid_pinned_requirements'):
            stager.verify(root)
        requirements.write_text(old_requirements)
        manifest.write_text(old_manifest)
        (root / 'wheels/example_dep-1.2.3-py3-none-any.whl').write_bytes(b'tampered')
        with self.assertRaisesRegex(deploy.DeployError, 'file_hash_mismatch'):
            stager.verify(root)

    def test_install_failure_prevents_preflight_and_switch(self):
        controller = self.fake_deployer()
        controller.stager.stage.side_effect = deploy.DeployError('install failed: private')
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertFalse(receipt['ok'])
        controller.preflight.check.assert_not_called()
        controller.switcher.activate.assert_not_called()
        self.assertNotIn('private', json.dumps(receipt))

    def test_preflight_failure_keeps_service_untouched(self):
        controller = self.fake_deployer()
        controller.preflight.check.side_effect = deploy.DeployError('private-config-details')
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertFalse(receipt['ok'])
        self.assertFalse(receipt['rolled_back'])
        controller.switcher.backup.assert_not_called()
        controller.switcher.activate.assert_not_called()
        self.assertNotIn('private', json.dumps(receipt))

    def test_success_orders_preflight_backup_switch_and_health(self):
        controller = self.fake_deployer()
        timeline = Mock()
        timeline.attach_mock(controller.preflight, 'preflight')
        timeline.attach_mock(controller.switcher, 'switcher')
        timeline.attach_mock(controller.health, 'health')
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertTrue(receipt['ok'])
        self.assertFalse(receipt['rolled_back'])
        self.assertEqual([call[0] for call in timeline.mock_calls],
            ['preflight.check', 'switcher.backup', 'preflight.drain', 'switcher.stop',
             'preflight.config_gate', 'preflight.drain', 'switcher.activate', 'health.wait', 'health.version'])

    def test_unhealthy_release_rolls_back_to_fixed_baseline_and_still_fails(self):
        controller = self.fake_deployer()
        controller.health.wait.side_effect = [{'healthy': False}, {'healthy': True}]
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertFalse(receipt['ok'])
        self.assertTrue(receipt['rolled_back'])
        self.assertTrue(receipt['health']['healthy'])
        self.assertEqual(controller.switcher.activate.call_args_list[-1].args, (self.settings.baseline,))

    def test_restart_failure_also_rolls_back_and_failed_rollback_is_reported(self):
        for rollback_fails in (False, True):
            controller = self.fake_deployer()
            controller.switcher.activate.side_effect = [deploy.DeployError('private restart details'),
                deploy.DeployError('private rollback details') if rollback_fails else None]
            receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
            self.assertFalse(receipt['ok'])
            self.assertEqual(receipt['rolled_back'], not rollback_fails)
            self.assertNotIn('private', json.dumps(receipt))
        controller = self.fake_deployer()
        controller.health.wait.side_effect = [{'healthy': False}, {'healthy': False}]
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertEqual(receipt['error'], 'rollback_health_failed')
        self.assertFalse(receipt['ok'])

    def test_explicit_rollback_uses_baseline_without_install_or_preflight(self):
        controller = self.fake_deployer()
        receipt = controller.execute('rollback', (BASELINE_SHA,))
        self.assertTrue(receipt['ok'])
        controller.stager.stage.assert_not_called()
        controller.preflight.check.assert_not_called()
        controller.switcher.activate.assert_called_once_with(self.settings.baseline)

    def test_preflight_uses_service_user_without_shell(self):
        release = self.settings.releases_dir / RELEASE
        deploy.Preflight(self.settings, self.runner).check(release)
        self.assertEqual(self.runner.calls, [['/usr/sbin/runuser', '-u', 'quant-ai', '--',
            str(release / 'venv/bin/quant-ai-service'), '--check-config', '--config', str(self.settings.config_path)]])

    def test_switcher_backs_up_exact_bytes_and_only_restarts_quant_unit(self):
        switcher = deploy.Switcher(self.settings, self.runner)
        path = self.settings.dropin_path
        path.parent.mkdir()
        path.write_bytes(switcher.render(self.settings.baseline))
        switcher.backup(RELEASE)
        release = self.settings.releases_dir / RELEASE
        (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/quant-ai-service').write_text('synthetic')
        switcher.activate(release)
        self.assertEqual(switcher.current(), release)
        self.assertEqual(path.with_name(path.name + '.backup-' + RELEASE).read_bytes(), switcher.render(self.settings.baseline))
        self.assertEqual(self.runner.calls, [['/usr/bin/systemctl', 'daemon-reload'],
                                             ['/usr/bin/systemctl', 'restart', 'quant-ai.service']])
        self.assertFalse(list(path.parent.glob('.50-approved*')))

    def test_health_requires_running_correct_execstart_and_both_status_codes(self):
        release = self.settings.releases_dir / RELEASE
        def unit(path=release, active='active', sub='running'):
            return f'ActiveState={active}\nSubState={sub}\nExecStart={{ path={path}/venv/bin/quant-ai-service ; argv[]=ignored ; }}\n'
        for output, discovery, mcp, expected in ((unit(), 200, 401, True), (unit(), 200, 200, False),
                (unit(), 302, 401, False), (unit(self.settings.baseline), 200, 401, False),
                (unit(active='inactive'), 200, 401, False), (unit(sub='exited'), 200, 401, False),
                ('ActiveState=active\nSubState=running\nExecStart=unknown', 200, 401, False)):
            request = Mock(side_effect=[discovery, mcp])
            health = deploy.HealthProbe(self.settings, FakeRunner(output), request=request)
            self.assertEqual(health.check(release)['healthy'], expected)
        self.assertEqual(request.call_args_list[0].args, ('http://127.0.0.1:8091' + deploy.DISCOVERY, 'GET'))
        self.assertEqual(request.call_args_list[1].args, ('http://127.0.0.1:8091/mcp', 'POST'))

    def test_health_retries_are_bounded_and_status_does_not_restart(self):
        ticks = [0]
        def sleep(seconds):
            ticks[0] += seconds
        health = deploy.HealthProbe(self.settings, self.runner, clock=lambda: ticks[0], sleep=sleep)
        health.check = Mock(return_value={'healthy': False})
        self.assertFalse(health.wait(self.settings.baseline)['healthy'])
        self.assertEqual(ticks[0], 45)
        controller = self.fake_deployer()
        controller.health.unit.return_value = (True, self.settings.baseline)
        controller.health.check.return_value = {'healthy': True, 'active_release_dir': str(self.settings.baseline)}
        self.assertTrue(controller.execute('status')['ok'])
        controller.switcher.activate.assert_not_called()

    def test_runner_suppresses_subprocess_output_and_does_not_inherit_secrets(self):
        with patch('quant_ai_deploy.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'fake', output='secret', stderr='secret')) as run:
            with self.assertRaisesRegex(deploy.DeployError, '^command_failed$'):
                deploy.Runner().run(['/usr/bin/false'])
        self.assertEqual(run.call_args.kwargs['env'], deploy.SAFE_ENV)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_main_produces_one_json_line_and_rejects_concurrent_commands(self):
        descriptor = self.settings.lock_path.open('w')
        self.addCleanup(descriptor.close)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = io.StringIO()
        with patch('quant_ai_deploy.Settings.load', return_value=self.settings), \
             patch('quant_ai_deploy.os.geteuid', return_value=0), \
             patch.dict('os.environ', {'SSH_ORIGINAL_COMMAND': 'status'}), \
             patch('sys.argv', ['quant-ai-deploy']), contextlib.redirect_stdout(output):
            code = deploy.main()
        self.assertEqual(code, 1)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())['error'], 'deployment_busy')

    def test_root_owned_settings_override_paths_but_cannot_change_service(self):
        path = self.root / 'deploy-baseline.json'
        path.write_text(json.dumps({'releases_dir': str(self.root / 'alternate'), 'port': 9000,
                                   'baseline_release': BASELINE, 'baseline_source_sha': BASELINE_SHA,
                                   'config_sha256': 'a' * 64}))
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)
        with patch.object(Path, 'lstat', return_value=info):
            settings = deploy.Settings.load(path)
        self.assertEqual(settings.releases_dir, self.root / 'alternate')
        self.assertEqual(settings.port, 9000)
        for value in ({'releases_dir': '/tmp/%injection'}, {'service': 'personal-ai.service'},
                      {'config_path': '/tmp/../private'}, {'port': True}, {'baseline_release': '../escape'}):
            path.write_text(json.dumps(value))
            with patch.object(Path, 'lstat', return_value=info), self.assertRaises(deploy.DeployError):
                deploy.Settings.load(path)
        for uid, mode in ((501, 0o600), (0, 0o640), (0, 0o666)):
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode)):
                with self.assertRaises(deploy.DeployError):
                    deploy.Settings.load(path)



class ClientTests(unittest.TestCase):
    def test_public_probe_requires_exact_codes_and_uses_no_auth(self):
        ticks = [0]
        def sleep(seconds):
            ticks[0] += seconds
        request = Mock(side_effect=lambda url, method: 200 if url.endswith(deploy.DISCOVERY) else 401)
        result = external_check('quant.example', request=request, clock=lambda: ticks[0], sleep=sleep)
        self.assertTrue(result['ok'])
        self.assertEqual(request.call_args_list[0].args, ('https://quant.example' + deploy.DISCOVERY, 'GET'))
        self.assertEqual(request.call_args_list[1].args, ('https://quant.example/mcp', 'POST'))
        result = external_check('quant.example', request=lambda *_: 200, clock=lambda: ticks[0], sleep=sleep)
        self.assertFalse(result['ok'])
        self.assertEqual(ticks[0], 45)
        for value in ('https://user:secret@example.com', 'https://example.com/mcp', 'http://example.com', 'example.com#fragment'):
            with self.assertRaises(ValueError):
                public_origin(value)

    def test_receipt_must_be_healthy_and_match_requested_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receipt.json'
            receipt = {'command': 'deploy', 'service': 'quant-ai.service', 'ok': True, 'rolled_back': False, 'requested_release': RELEASE,
                       'health': {'healthy': True, 'active_release_dir': '/opt/releases/' + RELEASE}}
            path.write_text(json.dumps(receipt) + '\n')
            self.assertEqual(validate_receipt(path, 'deploy', RELEASE), receipt)
            for change in ({'ok': False}, {'rolled_back': True}, {'requested_release': BASELINE},
                           {'health': {'healthy': False}}, {'command': 'rollback'}):
                path.write_text(json.dumps({**receipt, **change}) + '\n')
                with self.assertRaises(ValueError):
                    validate_receipt(path, 'deploy', RELEASE)
            path.write_text(json.dumps(receipt) + '\nextra\n')
            with self.assertRaises(ValueError):
                validate_receipt(path, 'deploy', RELEASE)


class QuantGateTests(unittest.TestCase):
    setUp = DeploymentTests.setUp
    archive = DeploymentTests.archive
    valid_bundle = DeploymentTests.valid_bundle
    fake_deployer = DeploymentTests.fake_deployer

    def test_drain_rejects_unknown_active_expired_and_orphan_pending_work(self):
        path = self.settings.db_path
        gate = deploy.Preflight(self.settings, self.runner)
        with self.assertRaisesRegex(deploy.DeployError, 'drain_unknown'):
            gate.drain()
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE tasks(status TEXT); CREATE TABLE subscriptions(active INTEGER);'
                             'CREATE TABLE event_deliveries(status TEXT);')
        original = path.read_bytes()
        gate.drain()
        self.assertEqual(path.read_bytes(), original)
        for table, column, value in [('tasks', 'status', 'outcome_unknown'),
                                     ('tasks', 'status', 'cancel_requested'),
                                     ('subscriptions', 'active', 1),
                                     ('event_deliveries', 'status', 'pending')]:
            with sqlite3.connect(path) as db:
                db.execute(f'INSERT INTO {table}({column}) VALUES (?)', (value,))
            with self.subTest(table=table), self.assertRaisesRegex(deploy.DeployError, 'inflight_work'):
                gate.drain()
            with sqlite3.connect(path) as db:
                db.execute(f'DELETE FROM {table}')
        gate.drain()

    def test_config_drift_and_wrong_rollback_sha_prevent_switch(self):
        self.settings.config_path.write_bytes(b'{"changed":true}')
        with self.assertRaisesRegex(deploy.DeployError, 'config_drift'):
            deploy.Preflight(self.settings, self.runner).check(self.settings.baseline)
        self.assertEqual(self.runner.calls, [])
        controller = self.fake_deployer()
        self.assertFalse(controller.execute('rollback', ('0' * 40,))['ok'])
        controller.switcher.stop.assert_not_called()
        controller.switcher.activate.assert_not_called()

    def test_post_stop_inflight_resumes_old_service_without_switching(self):
        controller = self.fake_deployer()
        controller.preflight.drain.side_effect = [None, deploy.DeployError('inflight_work')]
        receipt = controller.execute('deploy', (RELEASE, 'a' * 64), io.BytesIO())
        self.assertFalse(receipt['ok'])
        controller.switcher.stop.assert_called_once()
        controller.switcher.resume.assert_called_once()
        controller.switcher.activate.assert_not_called()

    def test_root_settings_are_required_and_no_private_baseline_is_assumed(self):
        with self.assertRaisesRegex(deploy.DeployError, 'deploy_config_required'):
            deploy.Settings.load(self.root / 'absent')

    def test_bundle_provenance_rejects_pr_failed_wrong_sha_and_digest(self):
        _, digest, root = self.valid_bundle()
        directory = self.root / 'candidate'
        directory.mkdir()
        (directory / 'bundle.tar.gz').write_bytes((self.root / 'bundle.tar.gz').read_bytes())
        (directory / 'bundle.sha256').write_text(digest + '\n')
        (directory / 'SHA256SUMS').write_bytes((root / 'SHA256SUMS').read_bytes())
        sha = 'abcdef123456' + '0' * 28
        metadata = {'source_sha': sha, 'bundle_sha256': digest, 'version': '2.0.7',
                    'release_id': RELEASE, 'python': '3.12', 'platform': 'manylinux2014_x86_64'}
        (directory / 'release.json').write_text(json.dumps(metadata))
        run = {'head_sha': sha, 'event': 'push', 'head_branch': 'main', 'conclusion': 'success',
               'status': 'completed', 'path': '.github/workflows/ci.yml', 'id': 1, 'run_attempt': 2,
               'repository': {'full_name': 'example/quant-service'}}
        self.assertEqual(verify(directory, sha, digest, run, 'example/quant-service', 'main'), metadata)
        for changes in ({'event': 'pull_request'}, {'head_branch': 'unreviewed'}, {'conclusion': 'failure'},
                        {'head_sha': '0' * 40}, {'path': '.github/workflows/other.yml'}, {'id': 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify(directory, sha, digest, {**run, **changes}, 'example/quant-service', 'main')
        with self.assertRaises(ValueError):
            verify(directory, sha, '0' * 64, run, 'example/quant-service', 'main')
        (directory / 'bundle.tar.gz').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            verify(directory, sha, digest, run, 'example/quant-service', 'main')
