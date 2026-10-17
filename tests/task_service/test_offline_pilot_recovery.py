"""Frozen synthetic pilot across SQLite reopen and caller interruptions.

No HTTP server, credentials, model, broker, child process or real worker is used.
"""
import copy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_service.runtime import Conflict, Policy, SubmissionRejected, TaskRuntime
from ai_service.scenarios import pilot_cli
from ai_service.scenarios.pilot import digest, prepare, submit, verify
from test_task_runtime import CallbackProvider


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


class InterruptedWorker(BaseException):
    """Simulate process loss after durable submitting state, without a process."""


class SyntheticWorker(CallbackProvider):
    def __init__(self):
        super().__init__()
        self.interrupt = False
        self.reject = False
        self.poll_calls = 0
        self.cancel_calls = 0

    def submit(self, request, *, task_id, owner):
        if self.interrupt or self.reject:
            self.calls += 1
            if self.interrupt:
                raise InterruptedWorker()
            raise SubmissionRejected('synthetic worker rejected before starting')
        return super().submit(request, task_id=task_id, owner=owner)

    def poll(self, handle):
        self.poll_calls += 1
        raise AssertionError('a callback pilot must not fall back to polling')

    def cancel(self, handle):
        self.cancel_calls += 1


class LocalTaskClient:
    """Only a test adapter to the real SQLite runtime; never an HTTP client."""
    def __init__(self, runtime, project):
        self.runtime = runtime
        self.project = project
        self.submit_calls = 0
        self.accepted_task_id = None
        self.lose_reply = False

    def submit(self, request, idempotency_key):
        self.submit_calls += 1
        record = self.runtime.submit(self.project, idempotency_key, request)
        self.accepted_task_id = record['id']
        if self.lose_reply:
            raise TimeoutError('synthetic accepted response lost')
        return record

    def get(self, task_id):
        return self.runtime.get(self.project, task_id)


class OfflinePilotRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.instance = json.loads((ROOT / 'examples/pilots/instance.json').read_text())
        self.assertIs(self.instance['enabled'], False)
        # This approval exists only inside the synthetic test, never in service config.
        self.instance['enabled'] = True
        self.instance['approvals'] = {'activation_ref': 'offline-fixture-only'}
        self.manifest = json.loads((ROOT / 'examples/pilots/input.json').read_text())
        self.instance_path = self.directory / 'instance.json'
        self.input_path = self.directory / 'input.json'
        self.instance_path.write_text(json.dumps(self.instance))
        self.input_path.write_text(json.dumps(self.manifest))
        self.intent = self.directory / 'intent.json'
        self.verified_receipt = self.directory / 'verified.json'
        self.project = self.instance['project']
        self.occurrence = 'offline-recovery-fixture'
        self.worker = SyntheticWorker()
        self.forbidden = Mock()
        self.forbidden.submit.side_effect = AssertionError('no alternate provider')
        self.forbidden.poll.side_effect = AssertionError('no alternate provider')
        self.forbidden.cancel.side_effect = AssertionError('no alternate provider')
        self.providers = {'dot': self.worker, 'api': self.forbidden, 'vps': self.forbidden}
        self.timeout = self.instance['stages'][0]['limits']['timeout_seconds']
        self.policy = Policy({'agent': 'dot'}, {'dot': {'configured-dot'}},
                             max_active=1, max_timeout_seconds=self.timeout)
        self.runtime = self.reopen()
        self.client = LocalTaskClient(self.runtime, self.project)
        time_patch = patch('ai_service.runtime.time.time', return_value=NOW.timestamp())
        datetime_patch = patch('ai_service.scenarios.pilot.datetime', wraps=datetime)
        self.time_mock = time_patch.start()
        self.addCleanup(time_patch.stop)
        self.datetime_mock = datetime_patch.start()
        self.addCleanup(datetime_patch.stop)
        self.datetime_mock.now.return_value = NOW
        self.task = prepare(self.instance, self.manifest, occurrence=self.occurrence, submitted_at=NOW)
        self.output = {
            'schema_version': 'scenario.pilot-result.v1',
            'operation_key': self.task.spec['operation_key'],
            'input_manifest_sha256': digest(self.manifest),
            'result': {'synthetic': True, 'no_order': True, 'adoption_allowed': False,
                       'sample_count': 4, 'difference_sum': 5},
        }

    def tearDown(self):
        self.assertEqual(self.worker.poll_calls, 0)
        self.assertEqual(self.forbidden.mock_calls, [])

    def reopen(self):
        return TaskRuntime(self.directory / 'tasks.sqlite', self.providers, {self.project: self.policy})

    def restart(self):
        self.runtime = self.reopen()
        self.client.runtime = self.runtime

    def advance_past_deadline(self):
        later = NOW + timedelta(seconds=self.timeout + 1)
        self.time_mock.return_value = later.timestamp()
        self.datetime_mock.now.return_value = later

    def cli(self, command, *, receipt, task_id=None):
        arguments = ['pilot', command, '--instance', str(self.instance_path),
                     '--input', str(self.input_path), '--occurrence', self.occurrence,
                     '--submitted-at', NOW.isoformat(), '--receipt', str(receipt)]
        if task_id:
            arguments += ['--task-id', task_id, '--provider', 'dot']
        output = StringIO()
        with patch('sys.argv', arguments), patch('sys.stdout', output), \
                patch.object(pilot_cli, 'client_from_env', return_value=self.client):
            result = pilot_cli.main()
        return result, json.loads(output.getvalue())

    def complete(self, task_id):
        claimed = self.runtime.claim(self.project, task_id, 'dot', 'synthetic-worker', 'stable-attempt')
        return self.runtime.report(self.project, task_id, 'dot', 'synthetic-worker',
                                   'stable-attempt', claimed['revision'], 'completed', self.output)

    def assert_failed_verification(self, task_id, status):
        self.assertEqual(self.client.get(task_id)['status'], status)
        code, result = self.cli('verify', receipt=self.verified_receipt, task_id=task_id)
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(self.verified_receipt.exists())

    def trace(self, task_id):
        record = self.client.get(task_id)
        print('SYNTHETIC_LIFECYCLE ' + json.dumps({
            'test': self.id().rsplit('.', 1)[1], 'task_id': task_id,
            'operation_key': self.task.spec['operation_key'], 'input_sha256': digest(self.manifest),
            'status': record['status'], 'error': record['error'], 'worker_submissions': self.worker.calls,
            'worker_cancellations': self.worker.cancel_calls, 'worker_polls': self.worker.poll_calls,
            'events': [event['status'] for event in self.runtime.events(self.project, task_id)],
        }))

    def test_completed_task_and_bound_receipts_survive_runtime_reconstruction(self):
        self.assertEqual(self.task.spec['template'], 'protocol_probe')
        self.assertEqual(self.task.request['timeout_seconds'], self.timeout)
        self.assertEqual(self.task.spec['limits']['max_calls'], 1)
        self.assertEqual(self.task.spec['limits']['retry_count'], 0)
        self.assertEqual(self.task.spec['deadline'], (NOW + timedelta(seconds=self.timeout)).isoformat().replace('+00:00', 'Z'))
        code, submitted = self.cli('submit', receipt=self.intent)
        self.assertEqual(code, 0)
        task_id = submitted['task_id']
        intent = json.loads(self.intent.read_text())
        acknowledgment = json.loads(Path(str(self.intent) + '.submitted').read_text())
        self.assertEqual(intent['operation_key'], acknowledgment['operation_key'])
        self.assertEqual(intent['request_sha256'], digest(self.client.get(task_id)['request']))
        self.assertEqual(acknowledgment['task_id'], task_id)
        self.runtime.tick()
        self.runtime.claim(self.project, task_id, 'dot', 'synthetic-worker', 'stable-attempt')
        self.restart()
        self.assertEqual(self.client.get(task_id)['attempt'], 'stable-attempt')
        self.runtime.tick()
        self.assertEqual(self.complete(task_id)['status'], 'completed')
        self.restart()
        code, receipt = self.cli('verify', receipt=self.verified_receipt, task_id=task_id)
        self.assertEqual(code, 0)
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(receipt['task_id'], task_id)
        self.assertEqual(receipt['operation_key'], intent['operation_key'])
        self.assertEqual(receipt['input_manifest_sha256'], digest(self.manifest))
        self.assertEqual(receipt['output_sha256'], digest(self.output))
        self.assertEqual(json.loads(self.verified_receipt.read_text()), receipt)
        for path in [self.intent, Path(str(self.intent) + '.submitted'), self.verified_receipt]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.worker.calls, 1)
        self.trace(task_id)

    def test_lost_accepted_reply_keeps_intent_and_recovers_only_original_task(self):
        self.client.lose_reply = True
        code, result = self.cli('submit', receipt=self.intent)
        self.assertEqual((code, result['status']), (1, 'stopped'))
        task_id = self.client.accepted_task_id  # Known to the fixture, not recovered by an external lookup.
        self.assertIsNotNone(task_id)
        original_intent = self.intent.read_bytes()
        self.assertFalse(Path(str(self.intent) + '.submitted').exists())
        self.restart()
        code, _ = self.cli('submit', receipt=self.intent)
        self.assertEqual(code, 1)
        self.assertEqual(self.client.submit_calls, 1)
        self.assertEqual(self.intent.read_bytes(), original_intent)
        self.runtime.tick()
        self.complete(task_id)
        self.restart()
        code, receipt = self.cli('verify', receipt=self.verified_receipt, task_id=task_id)
        self.assertEqual(code, 0)
        self.assertEqual(receipt['task_id'], task_id)
        self.assertEqual(self.client.submit_calls, 1)
        self.assertEqual(self.worker.calls, 1)
        self.trace(task_id)

    def test_interrupted_provider_submission_is_unknown_and_never_restarted(self):
        record = submit(self.client, self.task)
        self.worker.interrupt = True
        with self.assertRaises(InterruptedWorker):
            self.runtime.tick()
        self.assertEqual(self.client.get(record['id'])['status'], 'submitting')
        self.restart()
        self.runtime.tick()
        self.assert_failed_verification(record['id'], 'outcome_unknown')
        self.assertEqual(self.client.get(record['id'])['error'], 'acceptance_unknown')
        self.runtime.tick()
        self.assertEqual(self.worker.calls, 1)
        # Existing callback recovery resolves the original id/attempt without another submit.
        self.complete(record['id'])
        receipt = verify(self.task, self.client.get(record['id']), task_id=record['id'], provider='dot')
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(self.worker.calls, 1)
        self.trace(record['id'])

    def test_queued_deadline_cancels_without_starting_any_worker(self):
        record = submit(self.client, self.task)
        self.advance_past_deadline()
        self.restart()
        self.runtime.tick()
        self.assert_failed_verification(record['id'], 'cancelled')
        self.assertEqual(self.client.get(record['id'])['error'], 'deadline_before_submission')
        self.assertEqual(self.worker.calls, 0)
        self.trace(record['id'])

    def test_running_deadline_keeps_cancel_uncertainty_across_reconstruction(self):
        record = submit(self.client, self.task)
        self.runtime.tick()
        self.runtime.claim(self.project, record['id'], 'dot', 'synthetic-worker', 'stable-attempt')
        self.advance_past_deadline()
        self.runtime.tick()
        self.assert_failed_verification(record['id'], 'cancel_requested')
        self.assertEqual(self.worker.cancel_calls, 1)
        self.restart()
        self.runtime.tick()
        self.assertEqual(self.worker.cancel_calls, 1)
        self.runtime.report(self.project, record['id'], 'dot', 'synthetic-worker',
                            'stable-attempt', 1, 'cancelled')
        self.assert_failed_verification(record['id'], 'cancelled')
        self.assertEqual(self.worker.calls, 1)
        self.trace(record['id'])

    def test_invalid_worker_output_persists_failure_and_never_writes_verified_receipt(self):
        record = submit(self.client, self.task)
        self.runtime.tick()
        self.runtime.claim(self.project, record['id'], 'dot', 'synthetic-worker', 'stable-attempt')
        invalid = copy.deepcopy(self.output)
        invalid['result']['difference_sum'] = 6
        self.runtime.report(self.project, record['id'], 'dot', 'synthetic-worker',
                            'stable-attempt', 1, 'completed', invalid)
        self.restart()
        self.assert_failed_verification(record['id'], 'invalid_output')
        record = self.client.get(record['id'])
        self.assertIsNone(record['output'])
        self.assertEqual(record['error'], 'output_schema_mismatch')
        self.runtime.tick()
        self.assertEqual(self.worker.calls, 1)
        self.trace(record['id'])

    def test_changed_materials_conflict_with_original_operation_and_receipt(self):
        record = submit(self.client, self.task)
        self.runtime.tick()
        self.complete(record['id'])
        modified = copy.deepcopy(self.manifest)
        modified['series'][-1] += 1
        changed = prepare(self.instance, modified, occurrence=self.occurrence, submitted_at=NOW)
        self.assertEqual(changed.spec['operation_key'], self.task.spec['operation_key'])
        with self.assertRaises(Conflict):
            submit(self.client, changed)
        self.input_path.write_text(json.dumps(modified))
        self.assert_failed_verification(record['id'], 'completed')
        with self.runtime.connection() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.assertEqual(self.worker.calls, 1)
        self.trace(record['id'])

    def test_definite_rejection_persists_failure_without_fallback(self):
        record = submit(self.client, self.task)
        self.worker.reject = True
        self.runtime.tick()
        self.restart()
        self.runtime.tick()
        self.assert_failed_verification(record['id'], 'failed')
        self.assertEqual(self.client.get(record['id'])['error'], 'submission_rejected')
        self.assertEqual(self.worker.calls, 1)
        self.trace(record['id'])
