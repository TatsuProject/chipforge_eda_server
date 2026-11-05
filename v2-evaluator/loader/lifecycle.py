"""Serialize handoff with durable backend fencing, including external cf-cancel."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import subprocess

from .install import environment, interpreter
from .security import InstallError


@contextmanager
def request_lock(root, rid):
    locks = Path(root) / 'loader/request-locks'
    locks.mkdir(exist_ok=True)
    with (locks / rid).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
