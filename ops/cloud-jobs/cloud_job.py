"""Deterministic cloud notification/probes; no model, shell, or trading actuator."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ops/quant-monitor'))
from quant_monitor_domain.briefing_dispatch import telegram_target_outcome  # noqa: E402
from quant_monitor_domain.runtime_digest import prepare_runtime_digest  # noqa: E402


class Refused(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, *, token=None, method='GET', body=None, headers=None):
    fields = dict(headers or {})
    if token:
        fields['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=body, headers=fields, method=method)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=20) as reply:
        data = reply.read(4 * 1024**2 + 1)
    if len(data) > 4 * 1024**2:
        raise Refused('response_limit')
    return data


class GCS:
    """Generation-bound JSON state, using only the job's existing identity."""
    def __init__(self, transport=request):
        self.transport = transport

    def token(self):
        body = self.transport(
            'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
            headers={'Metadata-Flavor': 'Google'})
        return json.loads(body)['access_token']

    def address(self, uri):
        parsed = urllib.parse.urlsplit(uri)
        if (parsed.scheme != 'gs' or not parsed.netloc or not parsed.path.strip('/')
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise Refused('object_identity')
        return urllib.parse.quote(parsed.netloc, safe=''), urllib.parse.quote(parsed.path[1:], safe='')

    def read(self, uri):
        bucket, name = self.address(uri)
        url = f'https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}'
        token = self.token()
        meta = json.loads(self.transport(url, token=token))
        generation = str(meta['generation'])
        if not generation.isdigit():
            raise Refused('object_generation')
        body = json.loads(self.transport(url + '?alt=media&generation=' + generation, token=token))
        if not isinstance(body, dict):
            raise Refused('object_schema')
        return body, generation

    def write(self, uri, payload, generation):
        bucket, name = self.address(uri)
        if not str(generation).isdigit():
            raise Refused('object_generation')
        url = (f'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o'
               f'?uploadType=media&name={name}&ifGenerationMatch={generation}')
        body = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        if len(body) > 4 * 1024**2:
            raise Refused('state_limit')
        result = json.loads(self.transport(url, token=self.token(), method='POST', body=body,
                                          headers={'Content-Type': 'application/json'}))
        return str(result['generation'])


def deliver(store, state_uri, event, chat, send):
    """Persist pending before send; a CAS conflict/crash never authorizes replay."""
    payload, generation = store.read(state_uri)  # Missing migration state is fatal.
    if payload.get('schema_version') != 'quant_monitor_alert_state.v1':
        raise Refused('ledger_schema')
    if not re.fullmatch('[0-9a-f]{64}', event):
        raise Refused('event_identity')
    deliveries = payload.get('deliveries', {})
    if not isinstance(deliveries, dict):
        raise Refused('ledger_schema')
    target = hashlib.sha256(chat.encode()).hexdigest()
    targets = deliveries.get(event, {})
    if not isinstance(targets, dict):
        raise Refused('ledger_schema')
    if payload.get('fingerprint') == event and not targets:
        return 'unknown'  # Preserve legacy suppression; do not invent sent proof.
    record = targets.get(target)
    if record is not None:
        if (not isinstance(record, dict) or set(record) != {'status', 'attempts'}
                or record['status'] not in {'pending', 'unknown', 'failed', 'sent'}
                or type(record['attempts']) is not int or not 1 <= record['attempts'] <= 2):
            raise Refused('ledger_schema')
        if record['status'] == 'sent':
            return 'duplicate'
        if record['status'] in {'pending', 'unknown'}:
            return 'unknown'
        if record['attempts'] >= 2:
            return 'failed'
    attempts = 1 if record is None else record['attempts'] + 1
    targets[target] = {'status': 'pending', 'attempts': attempts}
    deliveries[event] = targets
    payload['deliveries'] = deliveries
    generation = store.write(state_uri, payload, generation)  # Must succeed first.
    try:
        outcome = send()
    except Exception:
        outcome = 'unknown'
    if outcome not in {'sent', 'failed', 'unknown'}:
        outcome = 'unknown'
    targets[target] = {'status': outcome, 'attempts': attempts}
    try:
        store.write(state_uri, payload, generation)
    except Exception:
        return 'unknown'  # Durable pending remains; never re-send.
    return outcome


def notify(config, store, *, accepted=False, send=telegram_target_outcome, now=None):
    payload, _ = store.read(config['projection_uri'])
    prepared = prepare_runtime_digest(payload)
    # Bind the exact frozen projection, observation, day and paper target scope.
    sys.path.insert(0, str(ROOT))
    from scripts.consume_daily_briefing import _paper_scope_problem, _runtime_object_problem, _runtime_object_observed_at
    day, targets = config['day'], config['expected_target_keys']
    observed = _runtime_object_observed_at(config['projection_uri'])
    if (not prepared.get('ok') or not prepared.get('sendable')
            or _runtime_object_problem(config['projection_uri'], day) is not None
            or _paper_scope_problem(targets) is not None
            or observed is None or observed != datetime.fromisoformat(str(prepared['observed_at']).replace('Z', '+00:00'))
            or prepared['business_date'] != day or set(prepared['target_scope']) != set(targets)):
        raise Refused('projection_binding')
    if not accepted:
        return {'ok': True, 'mode': 'preview', 'outcome': 'not_sent'}
    age_limit = config.get('max_age_seconds')
    now = now or datetime.now(timezone.utc)
    if type(age_limit) is not int or not 60 <= age_limit <= 172800 or not 0 <= (now - observed).total_seconds() <= age_limit:
        raise Refused('stale_projection')
    token = os.environ.get('TELEGRAM_TOKEN', '')
    chat = config['chat_id']
    if not token or not isinstance(chat, str) or not re.fullmatch('-?[0-9]+', chat):
        raise Refused('route_missing')
    outcome = deliver(store, config['state_uri'], prepared['event_id'], chat,
                      lambda: send(text=prepared['text'], token=token, chat_id=chat))
    return {'ok': outcome in {'sent', 'duplicate'}, 'mode': 'send', 'outcome': outcome}


def monitor(config, store, *, now=None, probe=request):
    now = now or datetime.now(timezone.utc)
    checks = config.get('checks', [])
    heartbeats = config.get('heartbeats', [])
    if not checks and not heartbeats:
        raise Refused('empty_monitor')
    failures = 0
    for item in checks:
        url = urllib.parse.urlsplit(item['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
            raise Refused('probe_identity')
        try:
            probe(item['url'])
        except Exception:
            failures += 1
    for item in heartbeats:
        try:
            heartbeat, _ = store.read(item['uri'])
            stamp = datetime.fromisoformat(heartbeat['observed_at'].replace('Z', '+00:00'))
            max_age = item['max_age_seconds']
            if (type(max_age) is not int or not 60 <= max_age <= 172800 or stamp.tzinfo is None
                    or not 0 <= (now - stamp).total_seconds() <= max_age
                    or heartbeat.get('ok') is not True
                    or heartbeat.get('mode') != item['expected_mode']):
                failures += 1
        except Exception:
            failures += 1
    return {'ok': failures == 0, 'mode': 'monitor', 'failed_checks': failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['notify', 'monitor', 'observer'])
    parser.add_argument('--config-uri', required=True)
    args = parser.parse_args(argv)
    try:
        store = GCS()
        config, _ = store.read(args.config_uri)
        result = (notify(config, store, accepted=os.environ.get('NOTIFY_SEND_ACCEPTED') == 'true')
                  if args.mode == 'notify' else monitor(config, store))
        # A separate object, not the shared delivery ledger; no secret/body data.
        uri = config['heartbeat_uri']
        _, generation = store.read(uri)  # Pre-created at cutover, never auto-reset.
        store.write(uri, {**result, 'observed_at': datetime.now(timezone.utc).isoformat()}, generation)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 2
    except Exception:
        print(json.dumps({'ok': False, 'mode': args.mode, 'reason': 'cloud_job_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
