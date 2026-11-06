"""Durable asynchronous acceptance/install/queue and evaluator supervision."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
import sqlite3
import threading
import time
import uuid

from . import cgroups
from .install import Installer, provision_tools
from .lifecycle import fence, request_lock
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


# Old evaluator versions are deleted (state, escrow and the installed tree; the archive stays for a reinstall)
# after a new one is ready, before a new one's preflight, and this often. CF_V2_KEEP_OLD_EVALUATORS=1: never.
PRUNE_SECONDS = float(os.environ.get('CF_V2_PRUNE_SECONDS') or 600)
PRUNED_TREES = ('state', 'escrow', 'evaluators')


def _remove(tree):
    """Delete a tree the evaluator may have made read-only. Never follows symlinks out of it."""
    try:
        if tree.is_symlink() or tree.is_file():
            tree.unlink()
            return
        if not tree.is_dir():
            return
        os.chmod(tree, 0o755)
        for dirpath, dirnames, _ in os.walk(tree, followlinks=False):
            for d in dirnames:
                path = os.path.join(dirpath, d)
                if not os.path.islink(path):
                    try:
                        os.chmod(path, 0o755)
                    except OSError:
                        pass
    except OSError:
        pass
    shutil.rmtree(tree, ignore_errors=True)


# Evaluator progress states that can never become a response by waiting longer.
TERMINAL_STATES = frozenset({'done', 'failed', 'cancelled', 'error'})


def system_error(job, code, detail='Evaluator infrastructure unavailable; retry this request.',
                 title='Evaluator infrastructure failure'):
    # No scores for infrastructure/provisioning faults. Never include exception text or subprocess output.
    instance = 'urn:chipforge:eval:' + job['request_id']
    return json.dumps(dict(schema='cf-response/1', request_id=job['request_id'], instance=instance,
        created_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        submission={'submitter': job['submitter']}, mode=job['mode'], status='ERROR', score=None,
        reward_eligible=False, pending=[], tier_reached='T0', summary=detail, stages=[], logs=[], receipt=None,
        timing={}, problems=[dict(type='urn:chipforge:problem:npu-v2:' + code, title=title,
        detail=detail, instance=instance + '#problem-1', code=code, category='infrastructure', fault='system',
        exploit=False, retryable=True, severity='error', where={'tier': 'T0', 'stage': 'evaluator_install'},
        checked='loader provisioning and service supervision', metadata={})])).encode()


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
        self.last_cleanup = None          # the latest cleanup that deleted something (reported by health)
        self.prune_lock = threading.Lock()
        self.next_prune = 0.0
        with self.db() as db:
            db.executescript(SCHEMA)
            # cf-chain-commit/1 (BEACON.md): forwarded verbatim to the evaluator, which validates it.
            if 'chain_commit' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN chain_commit TEXT')

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
            if value == 'preflight':
                # preflight checks free disk: make room first (an old version's runs can fill it)
                self.prune('before installing ' + sha[:12], wait=True)
        try:
            sig = self.root / 'archives' / (sha + '.sig')
            result = self.installer.install(sha, sig.read_bytes() if sig.exists() else b'', phase)
            with self.db() as db:
                db.execute('UPDATE installs SET phase=?, unsigned=? WHERE sha=? AND attempt=?',
                           ('ready', result['unsigned'], sha, attempt))
            self.next_prune = 0.0          # the older versions can go once the service moved to this one
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

    def accept(self, fields):
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
        sha = self.archive(fields)
        with self.mutex, request_lock(self.root, rid):
            old = self.job(rid)
            if old:
                if (old['sha'], old['mode'], old['submitter'], old['design_hash'], old['chain_commit']) != (
                        sha, mode, submitter, design_hash, chain):
                    raise HTTPError(409, 'REQUEST_ID_CONFLICT', 'This request ID already names a different request.')
                if old['phase'] == 'fencing' or old['forwarded'] or old['error'] in (None, 'EVALUATION_CANCELLED'):
                    return old
                # Explicit retries of a system failure may repair the install and resubmit.
                attempt = self.request_install(sha)
                with self.db() as db:
                    db.execute("UPDATE jobs SET attempt=?,phase='installing_evaluator',response=NULL,response_type=NULL,error=NULL,forwarded=0 WHERE request_id=?", (attempt, rid))
            else:
                attempt = self.request_install(sha)
                dest = self.root / 'loader/uploads' / (uuid.uuid4().hex + '.zip')
                os.replace(design, dest)
                # Multipart tempfiles were closed before this call. Durability precedes acceptance.
                with dest.open('rb') as f:
                    os.fsync(f.fileno())
                sync_dir(dest.parent)
                with self.db() as db:
                    db.execute('INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,chain_commit) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (rid, sha, attempt, mode, submitter, design_hash, str(dest), 'installing_evaluator', time.time(), chain))
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
        with self.db() as db:
            db.execute("UPDATE jobs SET phase='done',response=?,response_type=?,error=? WHERE request_id=? AND phase!='done'",
                       (body, content_type, error, job['request_id']))

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
                    latest = self.latest_ready()
                    if latest:
                        self.service.ensure(latest)  # Pre-warm and restart even when idle.
                if time.monotonic() >= self.next_prune:
                    self.next_prune = time.monotonic() + PRUNE_SECONDS
                    threading.Thread(target=self.prune, daemon=True, name='evaluator-cleanup').start()
                if os.environ.get('CF_CGROUP_PARENT'):
                    cgroups.reap()
                self.tools_error = None
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
        if time.monotonic() < self.retry_after.get(job['request_id'], 0):
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
            if submitting:
                # Atomic with cf-cancel: a cancelled snapshot cannot forward a design later.
                with request_lock(self.root, job['request_id']):
                    with self.db() as db:
                        claimed = db.execute("UPDATE jobs SET forwarded=1 WHERE request_id=? AND phase NOT IN ('done','fencing')",
                                             (job['request_id'],)).rowcount
                    if not claimed:
                        return
                    status, headers, body = self.service.submit(job)
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
                with self.db() as db:
                    db.execute("UPDATE jobs SET phase=?,forwarded=1 WHERE request_id=? AND phase NOT IN ('done','fencing')", (phase, job['request_id']))
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

    def in_use(self):
        """Evaluator versions that must stay: the newest ready one, the one the service runs, and any a
        pending job or an install in progress needs."""
        keep = {self.latest_ready(), self.service.sha}
        with self.db() as db:
            keep |= {r[0] for r in db.execute("SELECT DISTINCT sha FROM jobs WHERE phase!='done'")}
            keep |= {r[0] for r in db.execute(
                "SELECT sha FROM installs i WHERE attempt=(SELECT MAX(attempt) FROM installs WHERE sha=i.sha) "
                "AND phase NOT IN ('ready','failed')")}
        keep |= {sha for sha, thread in self.install_threads.items() if thread.is_alive()}
        keep.discard(None)
        return keep

    def prune(self, why='periodic', wait=False):
        """Delete evaluator versions no longer in use: state/<sha> (the evaluator's runs and caches, which can
        reach tens of GB), escrow/<sha> and evaluators/<sha>. archives/<sha>.zip and .sig stay: a version asked
        for again is reinstalled from them (its ready marker goes first, so a request never finds it half
        deleted). Returns what was deleted, or None."""
        if os.environ.get('CF_V2_KEEP_OLD_EVALUATORS') == '1':
            return None
        if not self.prune_lock.acquire(blocking=wait):
            return None
        try:
            with self.mutex:
                keep = self.in_use()
                if not keep:
                    return None                    # nothing installed or needed yet: delete nothing
                victims = sorted({p.name for d in PRUNED_TREES if (self.root / d).is_dir()
                                  for p in (self.root / d).iterdir() if SHA.fullmatch(p.name) and p.name not in keep})
                for sha in victims:
                    marker = self.root / 'evaluators' / sha / '.cf-ready.json'
                    if marker.exists():
                        marker.parent.chmod(0o755)
                        marker.unlink()
            if not victims:
                return None
            before = shutil.disk_usage(self.root).free
            removed = []
            (self.root / 'locks').mkdir(parents=True, exist_ok=True)
            for sha in victims:
                with (self.root / 'locks' / (sha + '.install.lock')).open('a') as lock:
                    try:                           # being (re)installed right now: leave it (never wait: no deadlock)
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    with self.mutex:
                        if sha in self.in_use():
                            continue               # asked for again meanwhile: it reinstalls
                    for d in PRUNED_TREES:
                        _remove(self.root / d / sha)
                    removed.append(sha)
            if not removed:
                return None
            usage = shutil.disk_usage(self.root)
            self.last_cleanup = dict(at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), reason=why,
                                     evaluators=removed, freed_gib=round(max(0, usage.free - before) / 2**30, 1),
                                     free_gib=round(usage.free / 2**30, 1))
            print(f"evaluator cleanup ({why}): removed {len(removed)} old evaluator version(s) "
                  f"{[s[:12] for s in removed]}, freed {self.last_cleanup['freed_gib']} GiB, "
                  f"{self.last_cleanup['free_gib']} GiB free", file=sys.stderr, flush=True)
            return self.last_cleanup
        finally:
            self.prune_lock.release()

    def disk(self):
        """Free and total space where evaluations run, and the evaluator versions with data on it."""
        usage = shutil.disk_usage(self.root)
        on_disk = sorted({p.name for d in PRUNED_TREES if (self.root / d).is_dir()
                          for p in (self.root / d).iterdir() if SHA.fullmatch(p.name)})
        return dict(free_gib=round(usage.free / 2**30, 1), total_gib=round(usage.total / 2**30, 1),
                    evaluator_versions=len(on_disk))

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
                    disk=self.disk(), last_cleanup=self.last_cleanup)
