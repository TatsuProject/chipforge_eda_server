"""Leftover evaluator boxes (v2.0.1).

Every sibling container an evaluator service starts carries the label cf.v2.loader=<root id>: the
loader exports it in CF_DOCKER_LABELS and evaluators from v2.0.1 keep inherited labels on every box.
The loader holds this root's lock and runs at most one evaluator service, so while no service runs
a container with this label is a leftover (a crashed or replaced service, a recreated container).
A leftover keeps its CPUs and spoils the timing of later jobs (25 Oct: 13 boxes of a stopped run
survived ~95 h). Containers are found by this label only, never by name; nothing else is touched.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

LABEL = 'cf.v2.loader'
DOCKER_TIMEOUT_S = 120


def label(root):
    return f'{LABEL}={hashlib.sha256(str(Path(root).absolute()).encode()).hexdigest()[:16]}'


def with_label(env, root):
    """`env` with this root's label appended to CF_DOCKER_LABELS ("k=v,k=v")."""
    own = label(root)
    labels = [x.strip() for x in env.get('CF_DOCKER_LABELS', '').split(',') if x.strip()]
    return dict(env, CF_DOCKER_LABELS=','.join(labels + ([own] if own not in labels else [])))


def _docker(args, timeout=DOCKER_TIMEOUT_S):
    try:
        return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def reap(root, why, log=None):
    """Force-remove this root's labelled containers. Call only while no evaluator service runs.
    -> {'found', 'removed', 'left': [ids], 'verified': bool}; logs one line when anything was found
    or the listing failed. Never raises."""
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    flt = ['--filter', 'label=' + label(root)]
    listed = _docker(['ps', '-a', '-q', '--no-trunc', *flt])
    if listed is None or listed.returncode:
        got = dict(found=[], removed=[], left=[], verified=False)
    else:
        found = listed.stdout.split()
        for cid in found:
            _docker(['rm', '-f', cid])
        after = _docker(['ps', '-a', '-q', '--no-trunc', *flt]) if found else listed
        ok = after is not None and after.returncode == 0
        left = after.stdout.split() if ok else found
        got = dict(found=found, removed=[c for c in found if c not in left], left=left, verified=ok)
    if got['found'] or not got['verified']:
        text = f"leftover evaluator boxes ({why}): {len(got['found'])} found, {len(got['removed'])} removed"
        if got['left']:
            text += ', still present: ' + ' '.join(c[:12] for c in got['left'])
        if not got['verified']:
            text += ' (docker listing failed: not verified)'
        log(text)
    return got
