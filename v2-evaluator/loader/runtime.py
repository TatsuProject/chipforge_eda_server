"""Durable asynchronous acceptance/install/queue and evaluator supervision."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import http.client
import json
import os
from pathlib import Path
import math
import re
import shutil
import sys
import sqlite3
import threading
import time
import uuid

_poll_clock = time.monotonic    # the status-poll pace: its own clock, separate from the 429/503 back-off

from . import cgroups
from .install import Installer, provision_tools
from .lifecycle import fence, ops_api, request_lock
from .multipart import HTTPError
from .security import InstallError, SHA, digest, private_locations, sync_dir
from .supervisor import Service

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS installs (
 sha TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL, error TEXT,
 unsigned INTEGER, PRIMARY KEY(sha,attempt));
CREATE TABLE IF NOT EXISTS jobs (
 request_id TEXT PRIMARY KEY, sha TEXT NOT NULL, attempt INTEGER NOT NULL,
 mode TEXT NOT NULL, submitter TEXT NOT NULL, design_hash TEXT NOT NULL,
 design TEXT NOT NULL, phase TEXT NOT NULL, created REAL NOT NULL,
 response BLOB, response_type TEXT, error TEXT, forwarded INTEGER NOT NULL DEFAULT 0);
'''


# v2.0.6 ops columns (cf-eval-ops/1, INTERFACE.md). Added in place: older rows read NULL. Never part of a
# request's identity. The gateway reads only request_id/phase/created/sha from this table.
OPS_COLUMNS = (('deadline', 'REAL'), ('started', 'REAL'), ('finished', 'REAL'), ('stage', 'TEXT'),
               ('tier', 'TEXT'), ('progress', 'TEXT'), ('queue_position', 'INTEGER'))


# Evaluator progress states that can never become a response by waiting longer.
TERMINAL_STATES = frozenset({'done', 'failed', 'cancelled', 'error'})
TIMEOUT = 'EVALUATION_TIMEOUT'
CONFLICT = 'REQUEST_ID_CONFLICT'
TIMEOUT_CHECKED = 'the request\'s deadline (deadline_unix / time_limit_s / X-CF-Deadline)'
API = 'cf-eval-ops/1'
FEATURES = ['deadline', 'cancel', 'jobs', 'progress', 'outcome', 'retention']
STAGE = re.compile(r'[A-Za-z0-9_.:+/-]{1,96}\Z')
DECIMAL = re.compile(r'[0-9]{1,20}(\.[0-9]{1,9})?\Z')


def env_number(name, default):
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def max_job_seconds():
    """The longest any evaluation may run from acceptance (the gateway's CF_V2_MAX_JOB_SECONDS rule)."""
    return env_number('CF_V2_MAX_JOB_SECONDS', 27600) or 27600


def iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def decimal(text):
    """ASCII decimal seconds (cf-eval-ops/1 form) -> float, or None."""
    text = text.strip() if isinstance(text, str) else ''
    return float(text) if len(text) <= 32 and DECIMAL.fullmatch(text) else None


def deadline_of(fields, headers, rid, accepted, now=None):
    """Effective deadline (UNIX seconds) of a request, or None (INTERFACE.md section 1).

    Form fields deadline_unix / time_limit_s (from `accepted`, the loader's acceptance time) are an explicit
    API: malformed -> 400 INVALID_DEADLINE. The X-CF-Deadline header is honoured only when X-CF-Request-Id is
    absent or names this request, and only within (now - 1 d, now + 7 d) like the gateway; otherwise it is
    ignored, never a refusal (validators already send it). The earliest wins, clamped to accepted +
    CF_V2_MAX_JOB_SECONDS. A deadline already past is valid: the request is accepted and times out unstarted."""
    now = time.time() if now is None else now
    found = []
    for name in ('deadline_unix', 'time_limit_s'):
        if name not in fields:
            continue
        value = decimal(fields[name])
        if value is None or value <= 0 or (name == 'deadline_unix' and value > now + 7 * 86400) or (
                name == 'time_limit_s' and value > 7 * 86400):
            raise HTTPError(400, 'INVALID_DEADLINE', 'deadline_unix / time_limit_s must be positive decimal '
                            'seconds no more than 7 days ahead.')
        found.append(value if name == 'deadline_unix' else accepted + value)
    headers = headers or {}
    header, owner = headers.get('X-CF-Deadline'), headers.get('X-CF-Request-Id')
    if header is not None and (owner is None or owner.strip() == rid):
        value = decimal(header)
        if value is not None and now - 86400 < value < now + 7 * 86400:
            found.append(value)
    return min(min(found), accepted + max_job_seconds()) if found else None


def system_error(job, code, detail='Evaluator infrastructure unavailable; retry this request.',
                 title='Evaluator infrastructure failure', stage=None, retryable=True, metadata=None,
                 checked='loader provisioning and service supervision', tier=None):
    # No scores for infrastructure/provisioning faults. Never include exception text or subprocess output.
    instance = 'urn:chipforge:eval:' + job['request_id']
    now = time.time()
    created, started, deadline = job.get('created'), job.get('started'), job.get('deadline')
    # One stage and one tier everywhere in the body (outcome, problem location, metadata): the stage given, else
    # the one the run reached, else evaluator_install; the tier given, else the one reached once started, else T0.
    reached = stage or (job.get('stage') if started else None) or ('evaluator_install' if not started else 'running')
    if tier is None:
        tier = job.get('tier') if started and job.get('tier') in ('T0', 'T1', 'T2') else 'T0'
    timing = dict(accepted_at=iso(created), started_at=iso(started), finished_at=iso(now),
                  queued_s=None if created is None else round(max(0.0, (started or now) - created), 3),
                  ran_s=None if started is None else round(max(0.0, now - started), 3), deadline_unix=deadline)
    outcome = dict(fault='timeout' if code == TIMEOUT else 'system', code=code, stage=reached,
                   reason=detail[:160], started=bool(started))
    return json.dumps(dict(schema='cf-response/1', request_id=job['request_id'], instance=instance,
        created_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        submission={'submitter': job['submitter']}, mode=job['mode'], status='ERROR', score=None,
        reward_eligible=False, pending=[], tier_reached=tier, summary=detail, stages=[], logs=[], receipt=None,
        timing=timing, problems=[dict(type='urn:chipforge:problem:npu-v2:' + code, title=title,
        detail=detail, instance=instance + '#problem-1', code=code, category='infrastructure', fault='system',
        exploit=False, retryable=retryable, severity='error', where={'tier': tier, 'stage': reached},
        checked=checked, metadata=metadata or {})], outcome=outcome)).encode()


def past_deadline(job):
    return job.get('deadline') is not None and time.time() >= float(job['deadline'])


def timeout_error(job, stage, why=None):
    """The loader's own EVALUATION_TIMEOUT (same text as the evaluator's catalogue entry, INTERFACE.md 5)."""
    started = bool(job.get('started'))
    tier = job.get('tier') if started and job.get('tier') in ('T0', 'T1', 'T2') else 'T0'
    metadata = dict(stage=stage, started=started, deadline_unix=job.get('deadline'))
    if why:
        metadata['reason'] = why
    return system_error(job, TIMEOUT, title='Evaluation deadline reached', stage=stage, retryable=False,
                        detail=f'The evaluation reached its deadline during the {stage} stage and was stopped. '
                               'This is not a design fault: it is not scored.',
                        metadata=metadata, checked=TIMEOUT_CHECKED, tier=tier)


def clean_progress(doc):
    """The evaluator's 202 progress, kept to stage names and times (never values, metrics or scores)."""
    out = []
    items = doc.get('progress') if isinstance(doc, dict) else None
    for item in (items if isinstance(items, list) else [])[:200]:
        if not isinstance(item, dict) or not isinstance(item.get('stage'), str) or not STAGE.fullmatch(item['stage']):
            continue
        entry = {'stage': item['stage']}
        for key in ('started_unix', 'finished_unix'):
            value = item.get(key)
            entry[key] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value) else None
        attempt = item.get('attempt')
        entry['attempt'] = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None
        out.append(entry)
    return out


class Runtime:
    def __init__(self, root, salt, public_key='', allow_unsigned=False, modes=('T2',), allow_runc=False,
                 workers=1, cores=None, min_disk_gib=None):
        self.root = Path(root).absolute()
        (self.root / 'loader').mkdir(parents=True, exist_ok=True)
        private_locations(self.root, salt)
        (self.root / 'loader/uploads').mkdir(exist_ok=True)
        self.lock = (self.root / 'loader/loader.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.installer = Installer(self.root, salt, public_key, allow_unsigned, modes, allow_runc, min_disk_gib)
        self.service = Service(self.installer, workers, cores)
        self.allow_unsigned = allow_unsigned
        self.mutex = threading.RLock()
        self.install_threads = {}
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.tools_ready = False
        self.tools_error = None
        self.start_failures = {}
        self.retry_after = {}
        self.poll_after = {}
        with self.db() as db:
            db.executescript(SCHEMA)
            # cf-chain-commit/1 (BEACON.md): forwarded verbatim to the evaluator, which validates it.
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            if 'chain_commit' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN chain_commit TEXT')
            for name, kind in OPS_COLUMNS:
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            if 'sent' not in columns:
                # 1 once this loader has handed the row's request to an evaluator; never reset (a 429/503 resets
                # `forwarded`, not this). An evaluator record under an ID whose row was never sent belongs to an
                # earlier request whose row has since expired: never adopted. Rows from an older loader (which
                # kept every row forever) count as sent, so their behaviour is unchanged.
                db.execute('ALTER TABLE jobs ADD COLUMN sent INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE jobs SET sent=1')
        self.startup_sweep = True          # first dispatch pass: stale requests left by a restart are not resumed
        self.next_housekeeping = 0.0
        self.sweep_uploads()               # before any request is accepted: uploads of finished/unknown jobs

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'loader/http.sqlite', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        self.thread = threading.Thread(target=self.dispatch, daemon=True, name='evaluator-dispatch')
        self.thread.start()
        # Recover interrupted installs and uncompleted jobs using their original attempt IDs.
        with self.db() as db:
            rows = db.execute("SELECT sha,MAX(attempt) attempt FROM installs GROUP BY sha").fetchall()
        for row in rows:
            latest = self.install_status(row['sha'])
            if latest['phase'] not in ('ready', 'failed'):
                self.request_install(row['sha'])

    def close(self):
        self.stop_event.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join()
        # Do not leave install threads mutating a root after releasing its loader lock.
        for thread in list(self.install_threads.values()):
            thread.join()
        self.service.stop()
        self.lock.close()

    def latest_ready(self):
        """The newest installed evaluator whose ready marker is valid here, or None."""
        with self.db() as db:
            rows = db.execute("SELECT sha FROM installs WHERE phase='ready' ORDER BY rowid DESC").fetchall()
        return next((r['sha'] for r in rows if self.installer.ready(r['sha'])), None)

    def install_status(self, sha, attempt=None):
        with self.db() as db:
            row = (db.execute('SELECT * FROM installs WHERE sha=? ORDER BY attempt DESC LIMIT 1', (sha,))
                   if attempt is None else db.execute('SELECT * FROM installs WHERE sha=? AND attempt=?', (sha, attempt))).fetchone()
        return dict(row) if row else None

    def archive(self, fields):
        sha = fields.get('evaluator_sha256')
        if sha is not None and not SHA.fullmatch(sha):
            raise HTTPError(400, 'INVALID_EVALUATOR_HASH', 'Use a lowercase SHA-256 digest.')
        signature = fields.get('evaluator_sig', fields.get('signature', b''))
        if 'evaluator_sig' in fields and 'signature' in fields:
            raise HTTPError(400, 'INVALID_FIELD', 'Provide one detached signature.')
        archive = fields.get('evaluator_zip')
        if not archive and not sha:
            raise HTTPError(400, 'MISSING_EVALUATOR', 'Provide evaluator_zip or evaluator_sha256.')
        if archive:
            actual = digest(archive)
            # Hash mismatch is an install failure attached to the accepted job, never a miner score.
            # Store under the expected hash; verify() checks bytes before extraction/execution.
            sha = sha or actual
            with self.mutex:
                dest = self.root / 'archives' / (sha + '.zip')
                if not dest.exists() or (self.install_status(sha) or {}).get('phase') == 'failed':
                    os.replace(archive, dest)
                    with dest.open('rb') as f:
                        os.fsync(f.fileno())
                    sync_dir(dest.parent)
                elif digest(dest) != actual:
                    raise HTTPError(409, 'EVALUATOR_ARCHIVE_CONFLICT', 'Different archive already names this hash.')
        elif not (self.root / 'archives' / (sha + '.zip')).is_file():
            raise HTTPError(404, 'EVALUATOR_NOT_CACHED', 'Upload evaluator_zip for a new evaluator.')
        if signature:
            with self.mutex:
                sig = self.root / 'archives' / (sha + '.sig')
                # A signature-only retry can repair an already cached archive.
                tmp = sig.with_suffix('.sig.tmp')
                with tmp.open('wb') as f:
                    f.write(signature)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, sig)
                sync_dir(sig.parent)
        return sha

    def request_install(self, sha):
        with self.mutex:
            latest = self.install_status(sha)
            thread = self.install_threads.get(sha)
            if thread and thread.is_alive():
                return latest['attempt']
            if latest and latest['phase'] == 'ready' and self.installer.ready(sha):
                return latest['attempt']
            attempt = (latest['attempt'] + 1 if latest and latest['phase'] in ('failed', 'ready')
                       else latest['attempt'] if latest else 1)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO installs VALUES(?,?,?,NULL,NULL)', (sha, attempt, 'verify'))
            thread = threading.Thread(target=self.install, args=(sha, attempt), daemon=True, name='install-' + sha[:12])
            self.install_threads[sha] = thread
            thread.start()
            return attempt

    def install(self, sha, attempt):
        def phase(value):
            with self.db() as db:
                db.execute('UPDATE installs SET phase=? WHERE sha=? AND attempt=?', (value, sha, attempt))
            self.wakeup.set()
        try:
            sig = self.root / 'archives' / (sha + '.sig')
            result = self.installer.install(sha, sig.read_bytes() if sig.exists() else b'', phase)
            with self.db() as db:
                db.execute('UPDATE installs SET phase=?, unsigned=? WHERE sha=? AND attempt=?',
                           ('ready', result['unsigned'], sha, attempt))
        except Exception as exc:
            code = exc.code if isinstance(exc, InstallError) else 'EVALUATOR_INSTALL_FAILED'
            with self.db() as db:
                db.execute('UPDATE installs SET phase=?,error=? WHERE sha=? AND attempt=?', ('failed', code, sha, attempt))
            # Operator diagnostics: the code and where the private step log is, never its content.
            logs = self.installer.failure_logs(sha)
            print(f'install failed: evaluator={sha} code={code} log={logs[0] if logs else "none"}',
                  file=sys.stderr, flush=True)
        finally:
            self.wakeup.set()

    def accept(self, fields, headers=None):
        rid, mode = fields.get('request_id'), fields.get('mode', 'T2')
        submitter = fields.get('submitter', rid)
        if not isinstance(rid, str) or not ID.fullmatch(rid) or not isinstance(submitter, str) or not ID.fullmatch(submitter):
            raise HTTPError(400, 'INVALID_ID', 'request_id and submitter must be 1–96 safe ASCII characters.')
        if mode not in self.installer.modes:
            raise HTTPError(400, 'MODE_DISABLED', 'Choose an enabled mode from /health.')
        design = fields.get('design_zip')
        if not design:
            raise HTTPError(400, 'MISSING_DESIGN', 'design_zip is required.')
        design_hash = digest(design)
        chain = fields.get('chain_commit')
        if chain is not None:
            # Canonical text, so a re-sent identical object is the same request; the evaluator checks
            # the schema and that it names this design.
            try:
                obj = json.loads(chain)
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                raise HTTPError(400, 'INVALID_CHAIN_COMMIT', 'chain_commit must be a JSON object (cf-chain-commit/1).')
            chain = json.dumps(obj, sort_keys=True, separators=(',', ':'))
        old = self.job(rid)
        # Validated before anything is stored; not part of the request's identity (a re-sent request may only
        # tighten its deadline, never loosen or conflict).
        deadline = deadline_of(fields, headers, rid, old['created'] if old else time.time())
        sha = self.archive(fields)
        with self.mutex, request_lock(self.root, rid):
            old = self.job(rid)
            if old:
                if (old['sha'], old['mode'], old['submitter'], old['design_hash'], old['chain_commit']) != (
                        sha, mode, submitter, design_hash, chain):
                    raise HTTPError(409, 'REQUEST_ID_CONFLICT', 'This request ID already names a different request.')
                if deadline is not None and old['phase'] != 'done' and (old['deadline'] is None or deadline < old['deadline']):
                    with self.db() as db:
                        db.execute("UPDATE jobs SET deadline=? WHERE request_id=? AND phase!='done'", (deadline, rid))
                    old['deadline'] = deadline
                    self.wakeup.set()
                if old['phase'] == 'fencing' or old['forwarded'] or old['error'] in (None, 'EVALUATION_CANCELLED', TIMEOUT, CONFLICT):
                    return old
                # Explicit retries of a system failure may repair the install and resubmit. The finished job's
                # upload was deleted: the same bytes (design_hash matched) take its place.
                attempt = self.request_install(sha)
                os.replace(design, old['design'])
                with open(old['design'], 'rb') as f:
                    os.fsync(f.fileno())
                sync_dir(Path(old['design']).parent)
                # A retry keeps the tighten-only rule: the earlier of the stored and the re-sent deadline, so a
                # deadline first sent with the retry is enforced (and forwarded to a cf-eval-ops service).
                kept = [d for d in (old['deadline'], deadline) if d is not None]
                with self.db() as db:
                    db.execute("UPDATE jobs SET attempt=?,phase='installing_evaluator',response=NULL,response_type=NULL,error=NULL,forwarded=0,"
                               "started=NULL,finished=NULL,stage=NULL,tier=NULL,progress=NULL,queue_position=NULL,deadline=? WHERE request_id=?",
                               (attempt, min(kept) if kept else None, rid))
            else:
                attempt = self.request_install(sha)
                dest = self.root / 'loader/uploads' / (uuid.uuid4().hex + '.zip')
                os.replace(design, dest)
                # Multipart tempfiles were closed before this call. Durability precedes acceptance.
                with dest.open('rb') as f:
                    os.fsync(f.fileno())
                sync_dir(dest.parent)
                with self.db() as db:
                    db.execute('INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,chain_commit,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (rid, sha, attempt, mode, submitter, design_hash, str(dest), 'installing_evaluator', time.time(), chain, deadline))
        accepted = self.job(rid)
        # Installation can fail/complete before this handler returns. A new acceptance is
        # still 202; terminal state belongs to the subsequent status poll.
        accepted.update(response=None, response_type=None, phase='installing_evaluator')
        self.wakeup.set()
        return accepted

    def job(self, rid):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE request_id=?', (rid,)).fetchone()
        return dict(row) if row else None

    def finish(self, job, body, content_type='application/json', error=None):
        if error:
            with request_lock(self.root, job['request_id']):
                current = self.job(job['request_id'])
                if current['phase'] == 'done':
                    return
                if current['phase'] == 'fencing':
                    body, content_type, error = current['response'], current['response_type'], current['error']
                if current['forwarded']:
                    # Durable intent precedes backend mutation and the public terminal row. A crash
                    # anywhere after this commit resumes cleanup before any service can restart.
                    with self.db() as db:
                        db.execute("UPDATE jobs SET phase='fencing',response=?,response_type=?,error=? WHERE request_id=?",
                                   (body, content_type, error, job['request_id']))
                    drained = fence(self.root, self.installer.salt, current, body)
                    completed = (drained or {}).get('completed_response')
                    if completed:
                        body = json.dumps(completed).encode()
                        if not any(p.get('code') == error for p in completed.get('problems', [])):
                            error = None
                self._finish(job, body, content_type, error)
            return
        with request_lock(self.root, job['request_id']):
            if self.job(job['request_id'])['phase'] != 'fencing':
                self._finish(job, body, content_type, error)

    def _finish(self, job, body, content_type, error):
        self.retry_after.pop(job['request_id'], None)
        self.poll_after.pop(job['request_id'], None)
        with self.db() as db:
            db.execute("UPDATE jobs SET phase='done',response=?,response_type=?,error=?,finished=?,queue_position=NULL "
                       "WHERE request_id=? AND phase!='done'", (body, content_type, error, time.time(), job['request_id']))

    def fail(self, job, code):
        self.finish(job, system_error(job, code), error=code)

    def set_phase(self, rid, phase):
        with self.db() as db:
            db.execute("UPDATE jobs SET phase=? WHERE request_id=? AND phase NOT IN ('done','fencing')", (phase, rid))

    def dispatch(self):
        while not self.stop_event.is_set():
            try:
                if not self.tools_ready:
                    provision_tools(self.root)
                    self.tools_ready, self.tools_error = True, None
                with self.db() as db:
                    jobs = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE phase!='done' ORDER BY created")]
                # Never restart/prewarm the service while an interrupted terminal fence is pending.
                fencing = [j for j in jobs if j['phase'] == 'fencing']
                if fencing:
                    for job in fencing:
                        self.finish(job, job['response'], job['response_type'], job['error'])
                    continue
                # Deadlines first: a request past its deadline is never forwarded (nor resumed after a restart).
                if self.expire(jobs, self.startup_sweep):
                    self.startup_sweep = False
                    continue
                self.startup_sweep = False
                active = next((j for j in jobs if j['forwarded']), None)
                hold = None     # a refused job keeps its evaluator: other evaluators' jobs wait (no service switching)
                for job in jobs:
                    install = self.install_status(job['sha'], job['attempt'])
                    if not install or install['phase'] == 'failed':
                        self.fail(job, install['error'] if install else 'EVALUATOR_INSTALL_INTERRUPTED')
                        continue
                    if install['phase'] != 'ready':
                        continue
                    if job['phase'] == 'installing_evaluator':
                        self.set_phase(job['request_id'], 'queued')
                        job['phase'] = 'queued'
                    if active and active['request_id'] != job['request_id']:
                        if not job['forwarded']:
                            hold = hold or job['sha']     # an older waiting job keeps its evaluator
                        continue
                    if hold and hold != job['sha'] and not active:
                        continue      # the active job is always polled, whatever evaluator a waiting job holds
                    self.tick(job)
                    # Only an accepted job owns the service; a refused mode must not block ready modes.
                    current = self.job(job['request_id'])
                    active = current if current['forwarded'] and current['phase'] != 'done' else None
                    if current['phase'] != 'done' and not current['forwarded']:
                        hold = hold or job['sha']
                    elif current['phase'] == 'done' and hold:
                        break      # the slot freed with an older job waiting: the next pass offers it first, in order
                if not jobs:
                    with self.db() as db:
                        latest = db.execute("SELECT sha FROM installs WHERE phase='ready' ORDER BY rowid DESC LIMIT 1").fetchone()
                    if latest:
                        self.service.ensure(latest['sha'])  # Pre-warm and restart even when idle.
                if os.environ.get('CF_CGROUP_PARENT'):
                    cgroups.reap()
                self.tools_error = None
                if time.monotonic() >= self.next_housekeeping:
                    self.next_housekeeping = time.monotonic() + 60
                    self.housekeeping()
            except Exception:
                # Do not log private paths, manifests, subprocess output or secrets.
                self.tools_error = 'LOADER_INFRASTRUCTURE_UNAVAILABLE'
                with self.db() as db:
                    pending = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE phase!='done'")]
                fencing_pending = any(j['phase'] == 'fencing' for j in pending)
                for job in pending:
                    try:
                        if job['phase'] == 'fencing':
                            self.finish(job, job['response'], job['response_type'], job['error'])
                        elif not fencing_pending:
                            self.fail(job, self.tools_error)
                    except Exception:
                        # A fence started in this handler may also need another pass.
                        fencing_pending = True
            self.wakeup.wait(0.25)
            self.wakeup.clear()

    def tick(self, job):
        if time.monotonic() < self.retry_after.get(job['request_id'], 0) or \
                _poll_clock() < self.poll_after.get(job['request_id'], 0):
            return
        self.retry_after.pop(job['request_id'], None)
        try:
            if (job.get("forwarded") and self.service.sha == job["sha"]
                    and self.service.proc is not None and self.service.proc.poll() is not None):
                # Preserve a terminal, unscored result before restarting for the next request.
                self.fail(job, "EVALUATOR_SERVICE_EXITED")
                self.service.stop()
                return
            self.service.ensure(job['sha'])
            self.start_failures.pop(job['sha'], None)
            status, headers, body = self.service.request('GET', '/status/' + job['request_id'])
            submitting = status == 404
            if status in (200, 202) and not (job.get('sent') or job.get('forwarded')):
                # The evaluator holds a request under this ID that this row never sent: an earlier request whose
                # loader row expired (CF_V2_REQUEST_RETENTION_DAYS) while the evaluator kept its own. Its result
                # is another design's: never adopted, and the new design is never run under the reused ID.
                fresh = self.job(job['request_id'])
                if fresh and not (fresh['sent'] or fresh['forwarded']) and fresh['phase'] not in ('done', 'fencing'):
                    self.finish(fresh, system_error(
                        fresh, CONFLICT, title='Request ID already used', retryable=False,
                        detail='The evaluator already holds an earlier request under this request ID. Nothing was '
                               'evaluated or scored; resubmit under a new request ID.'), error=CONFLICT)
                    return
            if submitting:
                sent = self.forward(job)
                if sent is None:
                    return
                status, headers, body = sent
                job = self.job(job['request_id'])
            if status == 200:
                self.finish(job, body, next((v for k, v in headers.items() if k.lower() == 'content-type'), 'application/json'))
            elif status == 202:
                try:
                    state = json.loads(body).get('state', 'queued')
                except (ValueError, AttributeError):
                    raise InstallError('INVALID_EVALUATOR_PROGRESS') from None
                if state in TERMINAL_STATES:
                    # The evaluator holds the request as finished but sent no response (an older evaluator
                    # answers a queue row marked done without a result like this forever). Terminal ERROR
                    # for the caller; the job leaves the active slot, so the next request (and evaluator)
                    # is not blocked behind it.
                    self.finish(job, system_error(
                        job, 'EVALUATOR_RESULT_MISSING', title='Evaluation ended without a recorded result',
                        detail='The evaluator finished this evaluation without a result. Nothing was scored; '
                               'resubmit under a new request ID.'), error='EVALUATOR_RESULT_MISSING')
                    return
                phase = 'running' if state in ('running', 'leased') else 'queued'
                self.note_progress(job, phase, json.loads(body), headers)
            elif status in (429, 503):
                if submitting:
                    # A restarted evaluator has no accepted copy of this previously forwarded job.
                    with self.db() as db:
                        db.execute("UPDATE jobs SET phase='queued',forwarded=0 WHERE request_id=? AND phase NOT IN ('done','fencing')", (job['request_id'],))
                delay = 30
                value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
                if value is not None:
                    try:
                        delay = max(0, int(value))
                    except ValueError:
                        try:
                            delay = max(0, parsedate_to_datetime(value).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                self.retry_after[job['request_id']] = time.monotonic() + delay
            else:
                # Name the evaluator's public refusal code (e.g. CHAIN_COMMIT_REQUIRED), never its text.
                try:
                    why = str(json.loads(body)['error']['code'])
                except (ValueError, KeyError, TypeError):
                    why = ''
                why = why if re.fullmatch(r'[A-Z0-9_]{1,64}', why) else f'HTTP {status}'
                self.finish(job, system_error(job, 'EVALUATOR_SERVICE_REJECTED', title='Evaluator refused the request',
                                              detail=f'The evaluator refused this request ({why}). Fix the request '
                                                     'and resubmit under a new request ID.'),
                            error='EVALUATOR_SERVICE_REJECTED')
        except InstallError as exc:
            # EVALUATOR_NOT_READY here is the install no longer matching this loader (supervisor.ensure), not a RAM
            # refusal (that is the evaluator's HTTP 503 above): terminal, as before.
            if exc.code in ('EVALUATOR_SERVICE_EXITED', 'EVALUATOR_SERVICE_START_FAILED'):
                failures = self.start_failures.get(job['sha'], 0) + 1
                self.start_failures[job['sha']] = failures
                if failures < 3:
                    return
            self.fail(job, exc.code)
        except (OSError, http.client.HTTPException):
            pass  # Durable service status/idempotent submission recovers after restart.

    def forward(self, job):
        """Hand a request to its evaluator. Under the request lock (atomic with cf-cancel and with a re-sent,
        tighter deadline), from the row as it is now, not this pass's snapshot: the deadline forwarded is the
        current one, and a request whose deadline has passed is never handed over - checked under the lock, and
        again by Service.submit once the body is written, just before it is sent.
        -> the evaluator's (status, headers, body), or None when nothing was sent."""
        rid = job['request_id']
        with request_lock(self.root, rid):
            fresh = self.job(rid)
            if not fresh or fresh['phase'] in ('done', 'fencing'):
                return None
            if not past_deadline(fresh):
                with self.db() as db:
                    claimed = db.execute("UPDATE jobs SET forwarded=1,sent=1 WHERE request_id=? AND phase NOT IN ('done','fencing')",
                                         (rid,)).rowcount
                if not claimed:
                    return None
                result = self.service.submit(fresh)
                if result is not None:
                    return result
                # The deadline passed while the body was written: nothing was sent. The claim is undone.
                with self.db() as db:
                    db.execute('UPDATE jobs SET forwarded=?,sent=? WHERE request_id=?', (fresh['forwarded'], fresh['sent'], rid))
            if not fresh['forwarded']:
                self.time_out_unsent(fresh)
                return None
        # Forwarded earlier, but the evaluator no longer holds it (404) and its deadline has passed: it is not
        # handed over again; it ends as a timeout through the fence, like any forwarded request.
        stage = fresh.get('stage') if fresh.get('started') and fresh.get('stage') else 'queued'
        self.finish(fresh, timeout_error(fresh, stage), error=TIMEOUT)
        return None

    def time_out_unsent(self, job, why=None):
        """Terminal EVALUATION_TIMEOUT for a request never handed over (stage installing/queued). The caller
        holds the request lock. -> True when this call finished it."""
        stage = 'installing' if job['phase'] == 'installing_evaluator' else 'queued'
        with self.db() as db:
            n = db.execute("UPDATE jobs SET phase='done',response=?,response_type='application/json',error=?,"
                           "finished=?,queue_position=NULL WHERE request_id=? AND forwarded=0 AND phase NOT IN ('done','fencing')",
                           (timeout_error(job, stage, why), TIMEOUT, time.time(), job['request_id'])).rowcount
        if n:
            self.retry_after.pop(job['request_id'], None)
            self.poll_after.pop(job['request_id'], None)
            print(f'request {job["request_id"]} timed out unstarted ({stage})', file=sys.stderr, flush=True)
        return bool(n)

    def note_progress(self, job, phase, doc, headers):
        """A 202 from the evaluator: phase, and (cf-eval-ops/1 evaluators) the current stage, tier, queue position,
        start time and stage timeline, kept for /status and /jobs. Polled no faster than the evaluator's
        Retry-After (at most 5 s): one poll per 0.25 s filled service.log with 1.25 M lines."""
        number = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
        current = doc.get('current_stage')
        stage = current if isinstance(current, str) and STAGE.fullmatch(current) else None
        tier = doc.get('tier') if doc.get('tier') in ('T0', 'T1', 'T2') else (
            doc.get('stage') if doc.get('stage') in ('T0', 'T1', 'T2') else None)
        position = doc.get('queue_position')
        position = position if isinstance(position, int) and not isinstance(position, bool) and position > 0 else None
        started = number(doc.get('started_unix')) or job.get('started') or (time.time() if phase == 'running' else None)
        progress = clean_progress(doc)
        with self.db() as db:
            db.execute("UPDATE jobs SET phase=?,forwarded=1,stage=?,tier=?,queue_position=?,started=?,progress=? "
                       "WHERE request_id=? AND phase NOT IN ('done','fencing')",
                       (phase, stage or ('running' if phase == 'running' else 'queued'), tier,
                        None if phase == 'running' else position or 1, started,
                        json.dumps(progress, separators=(',', ':')) if progress else None, job['request_id']))
        value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
        try:
            delay = min(5.0, max(0.5, float(value))) if value is not None else 1.0
        except ValueError:
            delay = 1.0
        self.poll_after[job['request_id']] = _poll_clock() + delay

    def expire(self, jobs, startup=False):
        """Deadline enforcement in the loader (INTERFACE.md section 2). Not yet forwarded and past its deadline:
        terminal EVALUATION_TIMEOUT at stage queued/installing, never forwarded. Forwarded: an evaluator that
        enforces deadlines itself (cf-eval-ops/1) is given 90 s to answer first; an older one is stopped at the
        deadline through the fence (its boxes and stages killed). At start-up also the gateway's age rule:
        an unfinished request without a deadline older than CF_V2_MAX_JOB_SECONDS is not resumed.
        -> True when a job was finished (the caller re-reads the table)."""
        now, changed = time.time(), False
        for job in jobs:
            if job['phase'] in ('done', 'fencing'):
                continue
            deadline, why = job.get('deadline'), None
            if deadline is None:
                if not (startup and now - job['created'] > max_job_seconds()):
                    continue
                why = 'stale after restart: no deadline and older than the longest evaluation'
            elif now < deadline + (90 if job['forwarded'] and ops_api(self.root, job['sha']) else 0):
                continue
            if job['forwarded']:
                stage = job.get('stage') if job.get('started') and job.get('stage') else 'queued'
                self.finish(job, timeout_error(job, stage, why), error=TIMEOUT)
                changed = True
                continue
            with request_lock(self.root, job['request_id']):
                changed = self.time_out_unsent(job, why) or changed
        return changed

    def sweep_uploads(self):
        """Delete uploaded designs no unfinished request needs (finished, cancelled, timed out, or orphaned by a
        crash between upload and acceptance). Under the acceptance mutex, so a request being accepted, or a
        retried one getting its bytes back, is never touched."""
        with self.mutex:
            with self.db() as db:
                keep = {Path(r[0]).name for r in db.execute("SELECT design FROM jobs WHERE phase!='done'")}
            for path in (self.root / 'loader/uploads').iterdir():
                if path.name not in keep and path.is_file() and not path.is_symlink():
                    path.unlink(missing_ok=True)

    def housekeeping(self):
        """Every 60 s: uploads, request retention, and log caps. Best effort: never stops the dispatcher."""
        try:
            self.sweep_uploads()
            days = env_number('CF_V2_REQUEST_RETENTION_DAYS', 7)
            if days:
                # Request IDs are kept for de-duplication, then expire; unfinished requests never do.
                with self.mutex, self.db() as db:
                    db.execute("DELETE FROM jobs WHERE phase='done' AND COALESCE(finished, created) < ?",
                               (time.time() - days * 86400,))
            self.cap_logs()
        except Exception:
            print('loader housekeeping pass failed; retried in 60 s', file=sys.stderr, flush=True)

    def cap_logs(self):
        """service.log and workers.log of the running evaluator: copy-truncated at CF_V2_LOG_MAX_MIB (64), one .1
        kept (both are opened O_APPEND, so writers continue at the new end). A cf-eval-ops/1 evaluator caps its
        own logs; this covers older ones."""
        sha = self.service.sha
        if not sha or ops_api(self.root, sha):
            return
        limit = int(env_number('CF_V2_LOG_MAX_MIB', 64) * (1 << 20)) or (64 << 20)
        for name in ('service.log', 'workers.log'):
            path = self.root / 'state' / sha / name
            try:
                if path.is_symlink() or path.stat().st_size <= limit:
                    continue
                shutil.copyfile(path, path.with_name(name + '.1'))
                os.truncate(path, 0)
            except OSError:
                pass

    def queue_view(self, recent=3600):
        """Unfinished requests in dispatch order (plus those finished in the last `recent` s), the gateway's /jobs
        `view` shape plus queue_position and stage. Phase: queued (installing or waiting), running, done."""
        now = time.time()
        with self.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT request_id,phase,created,started,deadline,finished,stage,forwarded FROM jobs "
                "WHERE phase!='done' OR COALESCE(finished, created) >= ? ORDER BY created", (now - recent,))]
        out, position = [], 0
        for r in rows:
            if r['phase'] == 'done':
                phase, where, stage = 'done', None, None
            elif r['phase'] in ('running', 'fencing'):
                phase, where, stage = 'running', None, r['stage'] or 'running'
            else:
                position += 1
                phase, where = 'queued', position
                stage = 'installing' if r['phase'] == 'installing_evaluator' else 'queued'
            out.append(dict(request_id=r['request_id'], phase=phase, loader_phase=r['phase'], queue_position=where,
                            stage=stage, sent_at=iso(r['created']), started_at=iso(r['started']),
                            deadline=iso(r['deadline']), finished_at=iso(r['finished']), sent_unix=r['created'],
                            started_unix=r['started'], deadline_unix=r['deadline'], finished_unix=r['finished']))
        return out

    def disk(self):
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            return None
        return dict(path=str(self.root), free_gib=round(usage.free / (1 << 30), 2),
                    total_gib=round(usage.total / (1 << 30), 2), used_gib=round(usage.used / (1 << 30), 2))

    def health(self):
        with self.db() as db:
            ready = db.execute("SELECT COUNT(*) FROM installs WHERE phase='ready'").fetchone()[0]
        with self.db() as db:
            failed = db.execute("SELECT sha, error FROM installs i WHERE phase='failed' AND attempt="
                                "(SELECT MAX(attempt) FROM installs WHERE sha=i.sha) ORDER BY sha LIMIT 10").fetchall()
        install_failures = []
        for sha, code in failed:
            logs = self.installer.failure_logs(sha)
            install_failures.append(dict(evaluator_sha256=sha, error=code, log=str(logs[0]) if logs else None))
        alive = self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()
        service_alive = self.service.proc is not None and self.service.proc.poll() is None and self.service.healthy
        return dict(ready=bool(ready and alive and service_alive and self.tools_ready and not self.tools_error),
                    evaluator_not_ready=self.service.not_ready,
                    status='ok' if alive else 'degraded', active_evaluator=self.service.sha,
                    modes=list(self.installer.modes), allow_unsigned=self.allow_unsigned,
                    sandbox_override=self.installer.allow_runc, preflight_min_disk_gib=self.installer.min_disk_gib,
                    scope_backend='cfscope', install_failures=install_failures,
                    api=API, features=FEATURES, disk=self.disk(), queue=self.queue_counts())

    def queue_counts(self):
        with self.db() as db:
            rows = db.execute("SELECT phase, COUNT(*), MIN(created) FROM jobs WHERE phase!='done' GROUP BY phase").fetchall()
        counts = {r[0]: (r[1], r[2]) for r in rows}
        waiting = [counts[p][1] for p in ('installing_evaluator', 'queued') if p in counts]
        return dict(installing=counts.get('installing_evaluator', (0,))[0], queued=counts.get('queued', (0,))[0],
                    running=sum(counts.get(p, (0,))[0] for p in ('running', 'fencing')),
                    oldest_queued_s=round(time.time() - min(waiting), 1) if waiting else None)
