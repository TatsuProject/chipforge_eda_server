"""Operator cancellation of one evaluation request, run inside the evaluator container:

    docker compose exec v2-evaluator cf-cancel [--reason "short public reason"] [--by WHO] -- <request_id>
    docker compose exec v2-evaluator cf-cancel --all [--reason ...] [--by kill_switch]

v2.0.6: the loader also serves this over HTTP with the admin token (POST /cancel/<id>, POST /cancel-all),
calling the same cancel() below in-process. A request already handed to its evaluator is
cancelled BY that evaluator (service.py --cancel): its job gets a terminal ERROR
(EVALUATION_CANCELLED, system fault, never scored), a running stage and its boxes are stopped at the
worker's next heartbeat, and the loader's next status poll finishes the request and moves on. A
request the loader has not handed over yet is finished by the loader itself with the same code.
Either way the caller sees 200 + ERROR on its next poll; resubmit under a new request_id.
Exit status: 0 cancelled or already finished, 3 unknown request, 1 failure.
`--by` (operator | validator | kill_switch | watchdog) is kept in the response's problem metadata.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .install import environment, interpreter
from .lifecycle import fence, ops_api, request_lock
from .runtime import system_error

CODE = 'EVALUATION_CANCELLED'
BY = ('operator', 'validator', 'kill_switch', 'watchdog')


def cancelled_error(job, reason, by):
    # One stage in outcome, problem location and metadata: the stage reached once started, else queued; and
    # the tier reached once started (system_error's default).
    stage = (job.get('stage') if job.get('started') else None) or 'queued'
    return system_error(job, CODE, detail=f'The validator operator cancelled this evaluation ({reason}). '
                        'Nothing was scored; resubmit under a new request ID.', stage=stage,
                        title='Evaluation cancelled by the validator', metadata={'by': by, 'stage': stage})


def loader_db(root):
    db = sqlite3.connect(Path(root) / 'loader/http.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    return db


def evaluator_cancel(root, salt, job, reason, by='operator'):
    """-> (exit code, parsed output or None) of the job's evaluator `service.py --cancel`."""
    sha = job['sha']
    tree = Path(root) / 'evaluators' / sha
    service = tree / 'v2/deploy/service.py'
    if not service.is_file():
        return 3, None
    env = environment(Path(root), sha, Path(salt))
    proc = subprocess.run([interpreter(), '-B', str(service), '--state', env['CF_ORCH_SSD'], '--posix',
                           env['CF_ORCH_POSIX'], '--cancel', job['request_id'], '--reason', reason]
                          + (['--by', by] if ops_api(root, sha) else []),
                          cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    if 'unrecognized arguments' in proc.stderr:
        raise SystemExit(f'evaluator {sha[:12]} predates operator cancellation (no service.py --cancel); '
                         'install a newer signed evaluator')
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = None
    return proc.returncode, out


def cancel(root, salt, rid, reason, by='operator'):
    from .runtime import ID
    if not ID.fullmatch(rid):
        return 3, {'request_id': rid, 'cancelled': False, 'reason': 'unknown request ID'}
    by = by if by in BY else 'operator'
    with request_lock(root, rid):
        return _cancel(root, salt, rid, reason, by)


def cancel_all(root, salt, reason, by='kill_switch'):
    """Every unfinished request, oldest first (the kill switch). -> (exit code, summary): 0 when every one was
    cancelled or had already finished, 1 when any failed (named in `failed`; run it again)."""
    with loader_db(root) as db:
        rids = [r[0] for r in db.execute("SELECT request_id FROM jobs WHERE phase!='done' ORDER BY created")]
    out = {'cancelled': [], 'already_finished': [], 'failed': []}
    for rid in rids:
        try:
            code, result = cancel(root, salt, rid, reason, by)
        except (SystemExit, Exception):
            code, result = 1, None
        key = 'failed' if code not in (0, 3) else 'cancelled' if (result or {}).get('cancelled') else 'already_finished'
        out[key].append(rid)
    return (1 if out['failed'] else 0), out


def _cancel(root, salt, rid, reason, by='operator'):
    with loader_db(root) as db:
        job = db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone()
    if job is None:
        return 3, {'request_id': rid, 'cancelled': False, 'reason': 'unknown request ID'}
    job = dict(job)
    if job['forwarded'] or (job['phase'] == 'done' and
            (Path(root) / 'evaluators' / job['sha'] / 'v2/deploy/service.py').is_file()):
        body = (job['response'] if job['phase'] == 'fencing' else None) or cancelled_error(job, reason, by)
        if job['phase'] != 'done':
            with loader_db(root) as db:
                db.execute("UPDATE jobs SET phase='fencing',response=?,response_type='application/json',error=? WHERE request_id=? AND phase!='done'",
                           (body, job['error'] if job['phase'] == 'fencing' else CODE, rid))
        drained = fence(root, salt, job, body)
        if job['phase'] == 'done':
            return 0, {'request_id': rid, 'cancelled': False, 'drained': True, 'reason': 'already finished; its result stands'}
        completed = (drained or {}).get('completed_response')
        if completed and not any(p.get('code') == CODE for p in completed.get('problems', [])):
            with loader_db(root) as db:
                db.execute("UPDATE jobs SET phase='done',response=?,error=NULL,finished=? WHERE request_id=? AND phase='fencing'",
                           (json.dumps(completed).encode(), time.time(), rid))
            return 0, {'request_id': rid, 'cancelled': False, 'drained': True, 'reason': 'already finished; its result stands'}
        with loader_db(root) as db:
            db.execute("UPDATE jobs SET phase='done',finished=? WHERE request_id=? AND phase='fencing'", (time.time(), rid))
        return 0, {'request_id': rid, 'cancelled': True, 'by': 'evaluator', 'drained': True, 'status': 'ERROR', 'code': CODE}
    if job['phase'] == 'done':
        return 0, {'request_id': rid, 'cancelled': False, 'reason': 'already finished; its result stands'}
    if not (job.get('sent') or job['forwarded']):
        # Never handed to any evaluator (hand-over claims sent=1 under this same request lock): finished here,
        # without asking the evaluator. Under a reused ID (CF_V2_REQUEST_RETENTION_DAYS) the evaluator may still
        # hold an EARLIER request's record; its "already finished" is that request's, and its own retention
        # sweep could later drop it and let this row be dispatched after a reported cancellation.
        with loader_db(root) as db:
            n = db.execute("UPDATE jobs SET phase='done',response=?,response_type='application/json',error=?,finished=?,"
                           "queue_position=NULL WHERE request_id=? AND phase NOT IN ('done','fencing') AND forwarded=0 AND sent=0",
                           (cancelled_error(job, reason, by), CODE, time.time(), rid)).rowcount
        if n:
            return 0, {'request_id': rid, 'cancelled': True, 'by': 'loader', 'status': 'ERROR', 'code': CODE}
        with loader_db(root) as db:
            job = dict(db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone())
        if job['phase'] == 'done':
            return 0, {'request_id': rid, 'cancelled': False, 'reason': 'already finished; its result stands'}
    for _ in range(2):
        code, out = evaluator_cancel(root, salt, job, reason, by)
        if code == 0:
            return 0, dict(out or {}, request_id=rid, by='evaluator')
        if code != 3:
            raise SystemExit(f'evaluator cancel failed (exit {code}): {out}')
        # The evaluator does not know the request yet: finish it in the loader, unless it was handed
        # over meanwhile (then the evaluator cancels it on the second pass).
        with loader_db(root) as db:
            n = db.execute("UPDATE jobs SET phase='done',response=?,response_type='application/json',error=?,finished=? "
                           "WHERE request_id=? AND phase!='done' AND forwarded=0",
                           (cancelled_error(job, reason, by), CODE, time.time(), rid)).rowcount
        if n:
            return 0, {'request_id': rid, 'cancelled': True, 'by': 'loader', 'status': 'ERROR', 'code': CODE}
        with loader_db(root) as db:
            job = dict(db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone())
        if job['phase'] == 'done':
            return 0, {'request_id': rid, 'cancelled': False, 'reason': 'already finished; its result stands'}
    raise SystemExit('request changed state twice while cancelling; run cf-cancel again')


def clean_reason(text, default='operator request'):
    """Public reason text: printable ASCII only, at most 200 characters."""
    text = ''.join(c for c in (text or '') if ' ' <= c <= '~').strip()[:200]
    return text or default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('request_id', nargs='?')
    ap.add_argument('--all', action='store_true', help='cancel every unfinished request (the kill switch)')
    ap.add_argument('--reason', default='operator request', help='short public reason, kept in the response')
    ap.add_argument('--by', choices=BY, default=None, help='who cancels (kept in the response metadata)')
    a = ap.parse_args(argv)
    if a.all == (a.request_id is not None):
        ap.error('give one request_id, or --all')
    root = Path(os.environ.get('CF_V2_ROOT') or '/var/lib/chipforge-v2')
    salt = Path(os.environ.get('CF_EVALUATOR_SALT_FILE', str(root.parent / (root.name + '-secrets') / 'evaluator-salt.bin')))
    if a.all:
        code, out = cancel_all(root, salt, clean_reason(a.reason), a.by or 'kill_switch')
    else:
        code, out = cancel(root, salt, a.request_id, clean_reason(a.reason), a.by or 'operator')
    print(json.dumps(out))
    return code


if __name__ == '__main__':
    sys.exit(main())
