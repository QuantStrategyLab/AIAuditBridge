import tempfile
import unittest
from pathlib import Path

from ai_service.notifications import NativeNotifications
from ai_service.runtime import Policy, TaskRuntime
from ai_service.providers import GrokWebhookProvider
from test_native_providers import FakeTransport


class NativeNotificationTests(unittest.TestCase):
    def test_followup_notifies_existing_task_once_and_lost_receipt_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = FakeTransport()
            provider = GrokWebhookProvider('https://cursor.com/routine', 'synthetic-secret',
                                           'https://service.example/mcp', transport=transport)
            runtime = TaskRuntime(Path(temp) / 'state.sqlite', {'grok': provider},
                                  {'owner/repo': Policy({'agent': 'grok'}, {'grok': {'configured'}})})
            notifications = NativeNotifications(runtime)
            task = runtime.submit('owner/repo', 'task', {'mode': 'agent', 'model': 'configured',
                                                        'objective': 'Investigate', 'output_schema': {'type': 'object'}})
            runtime.tick()
            runtime.claim('owner/repo', task['id'], 'grok', 'bot', 'attempt')
            runtime.message('owner/repo', task['id'], 'update', {'text': 'Updated evidence'})
            transport.fail = True
            notifications.flush()
            notifications.flush()
            self.assertEqual(len(transport.calls), 2)
            with runtime.connection() as db:
                self.assertEqual(db.execute('SELECT status FROM native_notifications').fetchone()[0], 'outcome_unknown')
            self.assertEqual(runtime.get('owner/repo', task['id'])['status'], 'running')
            self.assertEqual(runtime.get('owner/repo', task['id'])['revision'], 2)


if __name__ == '__main__':
    unittest.main()
