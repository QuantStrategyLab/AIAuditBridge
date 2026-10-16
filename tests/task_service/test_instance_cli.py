"""Exercise instance control only against disposable, fictional local instances."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.scenarios import instance_cli


class InstanceControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'approved.json'
        source = Path(__file__).resolve().parents[2] / 'examples/pilots/protocol-probe/instance.json'
        self.value = json.loads(source.read_text())
        self.value.update(project='Acme/Project', id='manual-probe', config_revision='b' * 40)
        self.value['approvals'] = {'activation_ref': 'approval-20261016-123'}
        self.write()
        self.kw = dict(state_dir=self.root, instance=self.path, project='Acme/Project',
                       instance_id='manual-probe', activation_ref='approval-20261016-123')
        self.network = patch('socket.create_connection', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write(self):
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def control(self, action, **overrides):
        kw = {**self.kw, **overrides}
        if action != 'readback' and 'expected_sha256' not in kw:
            kw['expected_sha256'] = self.digest()
        return instance_cli.control(action, **kw)

    def audit(self):
        return [json.loads(line) for line in (self.root / 'instance-audit.jsonl').read_text().splitlines()]

    def test_readback_enable_disable_preserve_all_other_fields_and_audit(self):
        original = copy.deepcopy(self.value)
        before = self.path.read_bytes()
        first = self.control('readback')
        self.assertFalse(first['enabled'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(first['external_approval_verified'])
        enabled = self.control('enable', expected_sha256=first['instance_sha256'])
        self.assertTrue(enabled['enabled'])
        self.assertEqual(json.loads(self.path.read_text()), {**original, 'enabled': True})
        disabled = self.control('disable', expected_sha256=enabled['instance_sha256'])
        self.assertFalse(disabled['enabled'])
        self.assertEqual(json.loads(self.path.read_text()), original)
        self.assertEqual([line['status'] for line in self.audit()],
                         ['readback', 'intent', 'readback', 'intent', 'readback'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'instance-audit.jsonl').stat().st_mode & 0o777, 0o600)

    def test_noop_disable_still_has_durable_audit(self):
        self.control('disable')
        self.assertEqual([line['status'] for line in self.audit()], ['intent', 'readback'])
        self.assertFalse(json.loads(self.path.read_text())['enabled'])

    def test_exact_project_instance_and_existing_approval_required(self):
        before = self.path.read_bytes()
        for kw in ({'project': 'Acme/Other'}, {'instance_id': 'other'},
                   {'activation_ref': 'approval-other'}, {'activation_ref': ''}):
            with self.subTest(kw=kw), self.assertRaises(instance_cli.ControlError):
                self.control('enable', **kw)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / 'instance-audit.jsonl').exists())

    def test_missing_existing_approval_cannot_be_installed_by_cli(self):
        self.value.pop('approvals')
        self.write()
        with self.assertRaises(instance_cli.ControlError):
            self.control('enable')

    def test_offline_approval_markers_refused_even_when_they_match(self):
        for ref in ['offline-test-only', 'OFFLINE-only', 'fixture-approval', 'approval.test.1',
                    'example-approval', 'dry-run-approval', 'sample-approval']:
            self.value['approvals']['activation_ref'] = ref
            self.write()
            with self.subTest(ref=ref), self.assertRaises(instance_cli.ControlError):
                self.control('enable', activation_ref=ref)

    def test_fixture_revision_refused(self):
        self.value['config_revision'] = '0' * 40
        self.write()
        with self.assertRaises(instance_cli.ControlError):
            self.control('enable')

    def test_repository_and_fixture_directories_refused(self):
        for marker in ['.git', 'fixtures', 'examples', 'tests', 'offline']:
            parent = self.root / marker
            parent.mkdir(mode=0o700)
            target = self.root if marker == '.git' else parent
            path = target / 'instance.json'
            path.write_bytes(self.path.read_bytes())
            path.chmod(0o600)
            with self.subTest(marker=marker), self.assertRaises(instance_cli.ControlError):
                self.control('readback', state_dir=target, instance=path)
            if marker == '.git':
                parent.rmdir()

    def test_symlink_root_instance_and_ancestor_refused(self):
        alias = self.root / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(instance_cli.ControlError):
            self.control('readback', instance=alias)
        with tempfile.TemporaryDirectory() as temporary:
            linked = Path(temporary).resolve() / 'linked'
            linked.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(instance_cli.ControlError):
                self.control('readback', state_dir=linked, instance=linked / self.path.name)

    def test_hardlink_and_insecure_permissions_refused(self):
        alias = self.root / 'alias.json'
        os.link(self.path, alias)
        with self.assertRaises(instance_cli.ControlError):
            self.control('readback')
        alias.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(instance_cli.ControlError):
            self.control('readback')
        self.path.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaises(instance_cli.ControlError):
            self.control('readback')
        self.root.chmod(0o700)

    def test_outside_state_directory_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(instance_cli.ControlError):
                self.control('readback', state_dir=Path(temporary).resolve())

    def test_mutation_requires_current_digest(self):
        for digest in [None, '', 'x' * 64, '0' * 64]:
            with self.subTest(digest=digest), self.assertRaises(instance_cli.ControlError):
                self.control('enable', expected_sha256=digest)
        self.assertFalse(json.loads(self.path.read_text())['enabled'])

    def test_unsafe_audit_and_lock_paths_refused_before_mutation(self):
        for name in ['instance-audit.jsonl', '.instance-control.lock']:
            target = self.root / name
            target.unlink(missing_ok=True)
            target.symlink_to(self.path)
            with self.subTest(name=name), self.assertRaises(OSError):
                self.control('enable')
            target.unlink()
        self.assertFalse(json.loads(self.path.read_text())['enabled'])

    def test_failed_audit_intent_prevents_mutation(self):
        before = self.path.read_bytes()
        with patch.object(instance_cli, '_append', side_effect=OSError('private detail')):
            with self.assertRaises(OSError):
                self.control('enable')
        self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_audit_record_blocks_mutation(self):
        audit = self.root / 'instance-audit.jsonl'
        audit.write_bytes(b'{"event":')
        audit.chmod(0o600)
        with self.assertRaises(instance_cli.ControlError):
            self.control('enable')
        self.assertFalse(json.loads(self.path.read_text())['enabled'])
        self.assertEqual(audit.read_bytes(), b'{"event":')

    def test_failed_replace_keeps_original_and_audited_intent(self):
        before = self.path.read_bytes()
        with patch.object(instance_cli.os, 'replace', side_effect=OSError('private detail')):
            with self.assertRaises(OSError):
                self.control('enable')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.audit()[0]['status'], 'intent')
        self.assertEqual(list(self.root.glob('.instance-*')), [self.root / '.instance-control.lock'])

    def test_intent_directory_sync_failure_prevents_mutation(self):
        before = self.path.read_bytes()
        with patch.object(instance_cli, '_sync_directory', side_effect=OSError('sync failed')):
            with self.assertRaises(OSError):
                self.control('enable')
        self.assertEqual(self.path.read_bytes(), before)

    def test_parallel_mutations_with_one_digest_allow_only_one_writer(self):
        expected = self.digest()
        def attempt(action):
            try:
                return self.control(action, expected_sha256=expected)['status']
            except instance_cli.ControlError:
                return 'stale'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['enable', 'disable']))
        self.assertCountEqual(results, ['readback', 'stale'])
        self.assertEqual([line['status'] for line in self.audit()], ['intent', 'readback'])

    def test_concurrent_edit_detected_after_intent(self):
        append = instance_cli._append
        def change_after_intent(fd, record):
            append(fd, record)
            self.value['inputs']['manifest_ref'] = 'new-manifest'
            self.write()
        with patch.object(instance_cli, '_append', side_effect=change_after_intent):
            with self.assertRaises(instance_cli.ControlError):
                self.control('enable')
        self.assertFalse(json.loads(self.path.read_text())['enabled'])

    def test_cli_does_not_disclose_invalid_private_json(self):
        self.path.write_text('{private-invalid-content')
        output = io.StringIO()
        args = ['readback', '--state-dir', str(self.root), '--instance', str(self.path),
                '--project', self.kw['project'], '--instance-id', self.kw['instance_id'],
                '--activation-ref', self.kw['activation_ref']]
        with contextlib.redirect_stdout(output):
            self.assertEqual(instance_cli.main(args), 1)
        self.assertNotIn('private-invalid-content', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'stopped')

    def test_final_audit_failure_does_not_claim_rollback(self):
        append = instance_cli._append
        def fail_on_readback(fd, record):
            if record['status'] == 'readback':
                raise OSError('private detail')
            append(fd, record)
        args = ['enable', '--state-dir', str(self.root), '--instance', str(self.path),
                '--project', self.kw['project'], '--instance-id', self.kw['instance_id'],
                '--activation-ref', self.kw['activation_ref'], '--expected-sha256', self.digest()]
        output = io.StringIO()
        with patch.object(instance_cli, '_append', side_effect=fail_on_readback), contextlib.redirect_stdout(output):
            self.assertEqual(instance_cli.main(args), 1)
        self.assertTrue(json.loads(self.path.read_text())['enabled'])
        self.assertIn('read back', output.getvalue())
        self.assertNotIn('private detail', output.getvalue())
        self.assertTrue(self.control('readback')['enabled'])

    def test_no_service_configuration_or_authorization_edits(self):
        protected = self.root / 'service.json'
        original = b'{"grants":[],"projects":{}}'
        protected.write_bytes(original)
        protected.chmod(0o600)
        with self.assertRaises(instance_cli.ControlError):
            self.control('enable', instance=protected, expected_sha256=hashlib.sha256(original).hexdigest())
        self.control('enable')
        self.control('disable')
        self.assertEqual(protected.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
