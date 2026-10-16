#!/usr/bin/env python3
"""Build or verify a Quant bundle using public provenance and explicit digests."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from ci_bundle import bundle
from quant_ai_deploy import BundleStager, RELEASE_ID, Settings


def build(root, output, source_sha, run_id):
    if re.fullmatch(r'[0-9a-f]{40}', source_sha) is None or re.fullmatch(r'[0-9]+', run_id) is None:
        raise ValueError('invalid build identity')
    version = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version) is None:
        raise ValueError('invalid package version')
    output.mkdir()
    digest = bundle(root, output / 'bundle.tar.gz')
    date = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')
    metadata = {'source_sha': source_sha, 'version': version, 'bundle_sha256': digest,
                'release_id': f'{date}-quant-v{version}-{source_sha[:12]}-r{run_id}',
                'python': '3.12', 'platform': 'manylinux2014_x86_64'}
    (output / 'release.json').write_text(json.dumps(metadata, sort_keys=True) + '\n')
    (output / 'bundle.sha256').write_text(digest + '\n')
    (output / 'SHA256SUMS').write_bytes((root / 'SHA256SUMS').read_bytes())
    return metadata


def verify(directory, source_sha, digest, run, repository, branch):
    if re.fullmatch(r'[0-9a-f]{40}', source_sha) is None or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('full source SHA and approved bundle SHA256 required')
    if (run.get('head_sha') != source_sha or run.get('event') != 'push'
            or run.get('head_branch') != branch or run.get('conclusion') != 'success'
            or run.get('status') != 'completed' or run.get('path') != '.github/workflows/ci.yml'
            or run.get('repository', {}).get('full_name') != repository):
        raise ValueError('bundle must come from successful default-branch CI at the exact SHA')
    metadata = json.loads((directory / 'release.json').read_text())
    if (metadata.get('source_sha') != source_sha or metadata.get('bundle_sha256') != digest
            or metadata.get('python') != '3.12' or metadata.get('platform') != 'manylinux2014_x86_64'
            or re.fullmatch(RELEASE_ID, metadata.get('release_id', '')) is None
            or f'-{source_sha[:12]}-r' not in metadata['release_id']
            or not metadata['release_id'].endswith(f"-r{run['id']}{run['run_attempt']}")):
        raise ValueError('bundle provenance mismatch')
    archive = directory / 'bundle.tar.gz'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError('bundle hash mismatch')
    if (directory / 'bundle.sha256').read_text().strip() != digest:
        raise ValueError('bundle sidecar mismatch')
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        stager = BundleStager(Settings(), None)
        stager.extract(archive, root)
        stager.verify(root, metadata['release_id'])
        if (directory / 'SHA256SUMS').read_bytes() != (root / 'SHA256SUMS').read_bytes():
            raise ValueError('manifest sidecar mismatch')
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('build')
    create.add_argument('--root', type=Path, required=True)
    create.add_argument('--out', type=Path, required=True)
    create.add_argument('--sha', required=True)
    create.add_argument('--run-id', required=True)
    check = sub.add_parser('verify')
    check.add_argument('--directory', type=Path, required=True)
    check.add_argument('--sha', required=True)
    check.add_argument('--digest', required=True)
    check.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'build':
        result = build(args.root, args.out, args.sha, args.run_id)
    else:
        result = verify(args.directory, args.sha, args.digest, json.loads(args.run.read_text()),
                        os.environ['GITHUB_REPOSITORY'], os.environ['DEFAULT_BRANCH'])
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(f"release_id={result['release_id']}\nbundle_sha256={result['bundle_sha256']}\n")
    print(json.dumps(result, sort_keys=True))
