"""At-most-once Grok follow-up triggers; unknown receipt is never retried."""
from .runtime import SubmissionRejected


class NativeNotifications:
    def __init__(self, runtime):
        self.runtime = runtime
        with runtime.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS native_notifications ('
                       'seq INTEGER PRIMARY KEY, status TEXT NOT NULL)')

    def flush(self):
        # Single service dispatcher owns notification processing. Mark before sending:
        # after a crash, unknown delivery remains visible and cannot trigger a second run.
        with self.runtime.connection() as db:
            db.execute("UPDATE native_notifications SET status='outcome_unknown' WHERE status='sending'")
            events = db.execute("SELECT e.seq,e.status,t.id,t.owner,t.provider,t.revision "
                "FROM events e JOIN tasks t ON t.id=e.task_id "
                "LEFT JOIN native_notifications n ON n.seq=e.seq "
                "WHERE e.status IN ('message','cancel_requested') AND n.seq IS NULL "
                "AND t.status IN ('running','cancel_requested') ORDER BY e.seq LIMIT 100").fetchall()
        for event in events:
            provider = self.runtime.providers.get(event['provider'])
            if not callable(getattr(provider, 'notify', None)):
                continue
            with self.runtime.connection() as db:
                db.execute('INSERT OR IGNORE INTO native_notifications(seq,status) VALUES(?,?)', (event['seq'], 'sending'))
            status = 'accepted'
            try:
                provider.notify(task_id=event['id'], owner=event['owner'], change=event['status'], revision=event['revision'])
            except SubmissionRejected:
                status = 'rejected'
            except Exception:
                status = 'outcome_unknown'
            with self.runtime.connection() as db:
                db.execute('UPDATE native_notifications SET status=? WHERE seq=?', (status, event['seq']))
