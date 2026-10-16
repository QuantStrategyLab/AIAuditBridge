#!/usr/bin/env python3
"""Package an already-downloaded wheelhouse, with no network or credentials."""
import argparse
import hashlib
from pathlib import Path
import tarfile

from quant_ai_deploy import wheel_requirement


def bundle(root, output):
    wheels = sorted((root / 'wheels').glob('*.whl'))
    if not wheels or set((root / 'wheels').iterdir()) != set(wheels):
        raise ValueError('a nonempty wheel-only directory is required')
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels}
    requirements = root / 'requirements.txt'
    requirements.write_text(''.join(wheel_requirement(path.name, hashes[path]) + '\n' for path in wheels))
    paths = [requirements, *wheels]
    manifest = root / 'SHA256SUMS'
    manifest.write_text(''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n' for path in paths))
    with tarfile.open(output, 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for path in [*paths, manifest]:
            info = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            with path.open('rb') as incoming:
                archive.addfile(info, incoming)
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(bundle(args.root, args.out))
