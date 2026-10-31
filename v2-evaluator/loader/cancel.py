"""Operator cancellation of one evaluation request, run inside the evaluator container:

    docker compose exec v2-evaluator cf-cancel <request_id> [--reason "short public reason"]

There is deliberately no HTTP endpoint for this. A request already handed to its evaluator is
cancelled BY that evaluator (service.py --cancel): its job gets a terminal ERROR
(EVALUATION_CANCELLED, system fault, never scored), a running stage and its boxes are stopped at the
worker's next heartbeat, and the loader's next status poll finishes the request and moves on. A
request the loader has not handed over yet is finished by the loader itself with the same code.
Either way the caller sees 200 + ERROR on its next poll; resubmit under a new request_id.
Exit status: 0 cancelled or already finished, 3 unknown request, 1 failure.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from .install import environment, interpreter
from .runtime import system_error

CODE = 'EVALUATION_CANCELLED'


def loader_db(root):
    db = sqlite3.connect(Path(root) / 'loader/http.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    return db


def evaluator_cancel(root, salt, job, reason):
    """-> (exit code, parsed output or None) of the job's evaluator `service.py --cancel`."""
    sha = job['sha']
    tree = Path(root) / 'evaluators' / sha
    service = tree / 'v2/deploy/service.py'
    if not service.is_file():
        return 3, None
    env = environment(Path(root), sha, Path(salt))
    proc = subprocess.run([interpreter(), '-B', str(service), '--state', env['CF_ORCH_SSD'], '--posix',
                           env['CF_ORCH_POSIX'], '--cancel', job['request_id'], '--reason', reason],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    if 'unrecognized arguments' in proc.stderr:
        raise SystemExit(f'evaluator {sha[:12]} predates operator cancellation (no service.py --cancel); '
                         'install a newer signed evaluator')
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = None
    return proc.returncode, out


def cancel(root, salt, rid, reason):
    with loader_db(root) as db:
        job = db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone()
    if job is None:
        return 3, {'request_id': rid, 'cancelled': False, 'reason': 'unknown request ID'}
    job = dict(job)
    if job['phase'] == 'done':
        return 0, {'request_id': rid, 'cancelled': False, 'reason': 'already finished; its result stands'}
    for _ in range(2):
        code, out = evaluator_cancel(root, salt, job, reason)
        if code == 0:
            return 0, dict(out or {}, request_id=rid, by='evaluator')
        if code != 3:
            raise SystemExit(f'evaluator cancel failed (exit {code}): {out}')
        # The evaluator does not know the request yet: finish it in the loader, unless it was handed
        # over meanwhile (then the evaluator cancels it on the second pass).
        with loader_db(root) as db:
            n = db.execute("UPDATE jobs SET phase='done',response=?,response_type='application/json',error=? "
                           "WHERE request_id=? AND phase!='done' AND forwarded=0",
                           (system_error(job, CODE, detail=f'The validator operator cancelled this evaluation '
                                         f'({reason}). Nothing was scored; resubmit under a new request ID.',
                                         title='Evaluation cancelled by the validator'), CODE, rid)).rowcount
        if n:
            return 0, {'request_id': rid, 'cancelled': True, 'by': 'loader', 'status': 'ERROR', 'code': CODE}
        with loader_db(root) as db:
            job = dict(db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone())
        if job['phase'] == 'done':
            return 0, {'request_id': rid, 'cancelled': False, 'reason': 'already finished; its result stands'}
    raise SystemExit('request changed state twice while cancelling; run cf-cancel again')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('request_id')
    ap.add_argument('--reason', default='operator request', help='short public reason, kept in the response')
    a = ap.parse_args(argv)
    root = Path(os.environ.get('CF_V2_ROOT') or '/var/lib/chipforge-v2')
    salt = Path(os.environ.get('CF_EVALUATOR_SALT_FILE', str(root.parent / (root.name + '-secrets') / 'evaluator-salt.bin')))
    code, out = cancel(root, salt, a.request_id, a.reason.strip()[:200] or 'operator request')
    print(json.dumps(out))
    return code


if __name__ == '__main__':
    sys.exit(main())
