#!/usr/bin/env python3
"""Freeze explicit component venvs and hash downloaded CPython 3.13 amd64 wheels.

Usage: python3 regenerate-locks.py --wheel-dir DIR orchestrator=VENV numerics=VENV ...
Run with Python 3.13 on Linux amd64. Downloads stay outside the Docker context.
"""
import argparse
import email
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--wheel-dir', required=True, type=Path)
    ap.add_argument('venvs', nargs='+', help='component=venv-directory')
    args = ap.parse_args()
    if sys.version_info[:2] != (3, 13):
        ap.error('run with CPython 3.13 to select the correct wheels')
    allowed = {'orchestrator', 'numerics', 'workloads', 'physical', 'reference-sw', 'divisions'}
    freezes = {}
    for spec in args.venvs:
        name, path = spec.split('=', 1)
        if name not in allowed or name in freezes:
            ap.error(f'unknown or duplicate component: {name}')
        freezes[name] = subprocess.check_output([str(Path(path)/'bin/pip'), 'freeze'], text=True)
    requirements = sorted(set(r for s in freezes.values() for r in s.splitlines()))
    for req in requirements:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', req):
            ap.error(f'only exact public package versions are supported: {req}')
    # Different components can pin different versions of the same distribution.
    groups = []
    for req in requirements:
        for group in groups:
            if all(normalize(r.split('==')[0]) != normalize(req.split('==')[0])
                   and ('+cpu' in r) == ('+cpu' in req) for r in group):
                group.append(req)
                break
        else:
            groups.append([req])
    args.wheel_dir.mkdir(parents=True, exist_ok=True)
    for group in groups:
        index = 'https://download.pytorch.org/whl/cpu' if '+cpu' in group[0] else 'https://pypi.org/simple'
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-deps',
                        '--only-binary=:all:', '--index-url', index, '--dest', str(args.wheel_dir),
                        *group], check=True)
    hashes = {}
    for wheel in sorted(args.wheel_dir.glob('*.whl')):
        with zipfile.ZipFile(wheel) as z:
            meta = email.message_from_bytes(z.read(next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))))
        key = normalize(meta['Name']) + '==' + meta['Version']
        hashes.setdefault(key, set()).add(hashlib.file_digest(wheel.open('rb'), 'sha256').hexdigest())
    dest = Path(__file__).resolve().parent/'locks'
    dest.mkdir(exist_ok=True)
    for name, freeze in freezes.items():
        lines = ['# Generated from pip freeze; CPython 3.13, Linux x86_64 wheels only.']
        if '+cpu' in freeze:
            lines += ['--extra-index-url https://download.pytorch.org/whl/cpu']
        for req in freeze.splitlines():
            package, version = req.split('==')
            pins = hashes[normalize(package)+'=='+version]
            lines.append(req + ''.join(' --hash=sha256:'+h for h in sorted(pins)))
        (dest/(name+'.lock')).write_text('\n'.join(lines)+'\n')


if __name__ == '__main__':
    main()
