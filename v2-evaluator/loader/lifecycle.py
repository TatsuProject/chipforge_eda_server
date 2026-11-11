"""Serialize handoff with durable backend fencing, including external cf-cancel."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess

from .install import environment, interpreter
from .security import InstallError

OPS_MARKER = b'CF_EVAL_OPS_API = "cf-eval-ops/1"'
_ops_cache = {}


def ops_api(root, sha):
    """True when evaluator `sha`'s service.py speaks cf-eval-ops/1 (v2.0.6+: deadline_unix/accepted_unix fields,
    202 progress, --cancel --by, --cancel-all). Older evaluators refuse unknown fields and flags, so the loader
    sends them only to an evaluator carrying the marker (INTERFACE.md section 0). An install's tree never
    changes under its sha, so a found answer is cached."""
    key = (str(root), sha)
    if key not in _ops_cache:
        service = Path(root) / 'evaluators' / sha / 'v2/deploy/service.py'
        try:
            found = OPS_MARKER in service.read_bytes()
        except OSError:
            return False                 # not installed (yet): ask again next time
        _ops_cache[key] = found
    return _ops_cache[key]


@contextmanager
def request_lock(root, rid):
    """Exclusive per-request lock (loader, cf-cancel and the HTTP routes). Lock files are pruned under their own
    lock (prune_request_locks), so after locking the holder checks that its file is still the one at the path;
    if it was removed meanwhile it locks the file now there instead."""
    locks = Path(root) / 'loader/request-locks'
    locks.mkdir(exist_ok=True)
    path = locks / rid
    while True:
        lock = path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = os.stat(path).st_ino == os.fstat(lock.fileno()).st_ino
            except FileNotFoundError:
                current = False
        except BaseException:
            lock.close()
            raise
        if current:
            break
        lock.close()
    try:
        yield
    finally:
        lock.close()


def prune_request_locks(root, keep):
    """Remove the lock files of request IDs not in `keep` that nobody holds: each is locked without waiting and
    removed while held, so a holder never loses its lock (request_lock re-checks its file after locking)."""
    locks = Path(root) / 'loader/request-locks'
    try:
        paths = [p for p in locks.iterdir() if p.name not in keep]
    except FileNotFoundError:
        return
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.stat(path).st_ino == os.fstat(lock.fileno()).st_ino:
                    path.unlink()
        except OSError:
            continue        # held (BlockingIOError) or already gone: left for the next pass


def fence(root, salt, job, response):
    """Success means the evaluator durably fenced admission/recovery and verified its drain.

    Legacy evaluators without the hook retain their pre-fencing lifecycle behaviour.
    """
    tree = Path(root) / 'evaluators' / job['sha']
    service = tree / 'v2/deploy/service.py'
    if not service.is_file():
        if job['forwarded']:
            raise InstallError('EVALUATOR_FENCE_FAILED')
        return {'fenced': True, 'drained': True, 'jobs': []}
    if b'--fence-request' not in service.read_bytes():
        return {'fenced': True, 'drained': True, 'jobs': []}
    env = environment(Path(root), job['sha'], Path(salt))
    try:
        proc = subprocess.run([interpreter(), '-B', str(service), '--state', env['CF_ORCH_SSD'],
                               '--posix', env['CF_ORCH_POSIX'], '--fence-request', job['request_id']],
                              input=response, cwd=tree, env=env, capture_output=True, timeout=300)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        if (proc.returncode or not isinstance(out, dict)
                or out.get('fenced') is not True or out.get('drained') is not True):
            raise ValueError('unverified fence')
        return out
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        raise InstallError('EVALUATOR_FENCE_FAILED') from None
