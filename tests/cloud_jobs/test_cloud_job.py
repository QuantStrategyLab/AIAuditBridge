import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('cloud_job', Path(__file__).parents[2] / 'ops/cloud-jobs/cloud_job.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
EVENT = 'a' * 64


class Store:
    def __init__(self):
        self.state = {'schema_version': 'quant_monitor_alert_state.v1', 'deliveries': {}}
        self.generation = 1
        self.fail_at = None
        self.writes = 0

    def read(self, uri):
        return copy.deepcopy(self.state), str(self.generation)

    def write(self, uri, state, generation):
        self.writes += 1
        if str(self.generation) != generation or self.writes == self.fail_at:
            raise RuntimeError('conflict')
        self.state = copy.deepcopy(state)
        self.generation += 1
        return str(self.generation)


class DeliveryTests(unittest.TestCase):
    def test_pending_is_durable_before_send_and_sent_is_not_replayed(self):
        store = Store()
        def send():
            record = next(iter(store.state['deliveries'][EVENT].values()))
            self.assertEqual(record['status'], 'pending')
            return 'sent'
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', send), 'sent')
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('replay')), 'duplicate')

    def test_overlapping_execution_observes_pending_and_never_sends(self):
        store = Store()
        def first_send():
            self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('second send')), 'unknown')
            return 'sent'
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', first_send), 'sent')

    def test_claim_conflict_never_sends(self):
        store = Store()
        store.fail_at = 1
        with self.assertRaises(RuntimeError):
            job.deliver(store, 'state', EVENT, '123', lambda: self.fail('send'))

    def test_completion_conflict_retains_pending_and_holds_replay(self):
        store = Store()
        store.fail_at = 2
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: 'sent'), 'unknown')
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('replay')), 'unknown')

    def test_crash_timeout_is_not_replayed(self):
        store = Store()
        def timeout():
            raise TimeoutError()
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', timeout), 'unknown')
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('replay')), 'unknown')

    def test_explicit_failure_has_only_one_extra_attempt(self):
        store = Store()
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: 'failed'), 'failed')
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: 'failed'), 'failed')
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('retry')), 'failed')

    def test_migration_required_and_no_legacy_sent_invention(self):
        store = Store()
        store.state = {}
        with self.assertRaises(job.Refused):
            job.deliver(store, 'state', EVENT, '123', lambda: self.fail('send'))
        store.state = {'schema_version': 'quant_monitor_alert_state.v1', 'fingerprint': EVENT}
        self.assertEqual(job.deliver(store, 'state', EVENT, '123', lambda: self.fail('send')), 'unknown')

    def test_observer_detects_missing_unhealthy_future_and_preview_heartbeat(self):
        store = Store()
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        cfg = {'heartbeats': [{'uri': 'state', 'max_age_seconds': 900, 'expected_mode': 'send'}]}
        for stamp, ok, mode in [('2026-10-14T00:00:00Z', True, 'send'), ('2026-10-15T01:00:00Z', True, 'send'),
                                ('2026-10-15T00:00:00Z', False, 'send'), ('2026-10-15T00:00:00Z', True, 'preview')]:
            store.state = {'observed_at': stamp, 'ok': ok, 'mode': mode}
            self.assertFalse(job.monitor(cfg, store, now=now)['ok'])
        store.state = {'observed_at': now.isoformat(), 'ok': True, 'mode': 'send'}
        self.assertTrue(job.monitor(cfg, store, now=now)['ok'])

    def test_probe_failure_does_not_claim_service_health(self):
        cfg = {'checks': [{'url': 'https://example.com'}]}
        def unavailable(url):
            raise OSError()
        self.assertFalse(job.monitor(cfg, Store(), probe=unavailable)['ok'])

    def test_gcs_reads_pinned_generation_and_writes_compare_and_swap(self):
        calls = []
        def transport(url, **kwargs):
            calls.append((url, kwargs))
            if 'metadata.google.internal' in url:
                return b'{"access_token":"fake-not-a-secret"}'
            if 'upload' in url:
                return b'{"generation":"8"}'
            if 'alt=media' in url:
                return b'{"ok":true}'
            return b'{"generation":"7"}'
        store = job.GCS(transport)
        payload, generation = store.read('gs://bucket/state.json')
        store.write('gs://bucket/state.json', payload, generation)
        self.assertIn('generation=7', calls[2][0])
        self.assertIn('ifGenerationMatch=7', calls[-1][0])


if __name__ == '__main__':
    unittest.main()
