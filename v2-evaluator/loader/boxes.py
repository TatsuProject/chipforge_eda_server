"""Leftover evaluator boxes (v2.0.1).

Every sibling container an evaluator service starts carries the label cf.v2.loader=<root id>: the
loader exports it in CF_DOCKER_LABELS and evaluators from v2.0.1 keep inherited labels on every box.
The loader holds this root's lock and runs at most one evaluator service, so while no service runs
a container with this label is a leftover (a crashed or replaced service, a recreated container).
"No service runs" is established, not assumed (Codex review of v201): reap_unowned removes boxes only
while the loader itself holds the root's evaluation lock (every evaluation of the root holds it while
its boxes live) and the service's state lock; a restarted loader that meets a still-running evaluator
removes nothing.
A leftover keeps its CPUs and spoils the timing of later jobs (25 Oct: 13 boxes of a stopped run
survived ~95 h). Containers are found by this label only, never by name; nothing else is touched.
"""
import fcntl
import hashlib
import os
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


EVALUATION_LOCK = 'active-evaluation.lock'      # the evaluator's resources.HOST_LOCK_NAME under its data root


def owner_locks(root, state=None, environ=None):
    """The locks a live evaluator of this root holds: the evaluation lock (CF_V2_HOST_LOCK, else
    <root>/active-evaluation.lock: held by every evaluation for its whole life) and the service's state lock
    (<state>/http.lock: held by the service process)."""
    environ = os.environ if environ is None else environ
    out = [environ.get('CF_V2_HOST_LOCK') or str(Path(root) / EVALUATION_LOCK)]
    if state:
        out.append(str(Path(state) / 'http.lock'))
    return out


def reap_unowned(root, why, *, state=None, environ=None, log=None):
    """reap() only while this process holds every owner lock (owner_locks): the evaluation lock always (created when
    missing, so held during the listing and removal), the state lock when it exists. Another holder means a live
    evaluator (e.g. one started before this loader restarted): then nothing is removed.
    -> reap()'s dict, or with 'skipped': the lock that is held. Never raises."""
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    fds = []
    try:
        for n, path in enumerate(owner_locks(root, state, environ)):
            try:
                # the evaluation lock is created when missing (as the evaluator does, O_RDONLY 0666), so it is
                # always held while boxes are listed: no evaluation, however it started, can start a box then
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | (os.O_CREAT if n == 0 else 0), 0o666)
            except FileNotFoundError:
                if n == 0:                   # the evaluation lock cannot even be created: do not guess
                    log(f'leftover evaluator boxes ({why}) not removed: cannot create {path}')
                    return dict(found=[], removed=[], left=[], verified=False, skipped=path)
                continue                     # no state lock yet: no service ever ran there
            except OSError as e:
                log(f'leftover evaluator boxes ({why}) not removed: cannot open {path} ({e.strerror})')
                return dict(found=[], removed=[], left=[], verified=False, skipped=path)
            fds.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log(f'leftover evaluator boxes ({why}) not removed: a live evaluator holds {path}')
                return dict(found=[], removed=[], left=[], verified=False, skipped=path)
        return reap(root, why, log=log)
    finally:
        for fd in fds:
            os.close(fd)


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
