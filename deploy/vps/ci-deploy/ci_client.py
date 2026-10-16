#!/usr/bin/env python3
"""CI receipt validation and unauthenticated public HTTP probes."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from quant_ai_deploy import DISCOVERY, MCP, HealthProbe


def public_origin(host):
    origin = host if host.startswith('https://') else 'https://' + host
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('QUANT_PUBLIC_HOST must be an HTTPS hostname or origin')
    return origin.rstrip('/')


def validate_receipt(path, command, release_id=None):
    if path.stat().st_size > 32768:
        raise ValueError('receipt too large')
    lines = path.read_text().splitlines()
    if len(lines) != 1:
        raise ValueError('expected exactly one receipt line')
    receipt = json.loads(lines[0])
    if (not isinstance(receipt, dict) or receipt.get('command') != command
            or receipt.get('service') != 'quant-ai.service'
            or receipt.get('ok') is not True or receipt.get('health', {}).get('healthy') is not True):
        raise ValueError('remote command did not report healthy success')
    if command == 'deploy':
        active = receipt['health'].get('active_release_dir', '')
        if (receipt.get('requested_release') != release_id or Path(active).name != release_id
                or receipt.get('rolled_back') is not False):
            raise ValueError('deployment receipt does not match requested release')
    return receipt


def external_check(host, *, request=HealthProbe.http_status, clock=time.monotonic, sleep=time.sleep):
    origin = public_origin(host)
    deadline = clock() + 45
    while True:
        result = {'command': 'external_check', 'ok': False}
        try:
            result['discovery_status'] = request(origin + DISCOVERY, 'GET')
            result['mcp_status'] = request(origin + MCP, 'POST')
            result['ok'] = result['discovery_status'] == 200 and result['mcp_status'] == 401
        except (OSError, ValueError):
            result['error'] = 'public_probe_failed'
        if result['ok'] or clock() >= deadline:
            return result
        sleep(min(2, max(0, deadline - clock())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='action', required=True)
    receipt_parser = subparsers.add_parser('receipt')
    receipt_parser.add_argument('path', type=Path)
    receipt_parser.add_argument('command', choices=['deploy', 'rollback', 'status'])
    receipt_parser.add_argument('--release-id')
    external_parser = subparsers.add_parser('external')
    external_parser.add_argument('host')
    external_parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'receipt':
        try:
            validate_receipt(args.path, args.command, args.release_id)
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            raise SystemExit('Remote receipt rejected; see the receipt artifact') from None
    else:
        result = external_check(args.host)
        args.out.write_text(json.dumps(result) + '\n')
        raise SystemExit(0 if result['ok'] else 1)
