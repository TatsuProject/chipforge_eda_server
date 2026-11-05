"""Loopback-only evaluator process, restarted after a crash; one active evaluator."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from . import boxes, cgroups
from .install import environment, interpreter
from .security import InstallError, atomic_json

LOG_ROTATE_BYTES = 64 << 20


def exit_text(returncode):
    """'returncode 1' / 'signal 9 (SIGKILL)' for a finished process."""
    if returncode is not None and returncode < 0:
        try:
            return f'signal {-returncode} ({signal.Signals(-returncode).name})'
        except ValueError:
            return f'signal {-returncode}'
    return f'returncode {returncode}'


def report_exit(sha, proc, log_path, when):
    """v2.0.1: the loader's own log (docker logs) names an evaluator service that ended by itself, how it
    ended, and where its own log and its workers' log are. Paths only, never their content."""
    print(f'evaluator service {sha[:12]} pid {proc.pid} exited {when}: {exit_text(proc.returncode)}; '
          f'service log {log_path}; worker log {Path(log_path).with_name("workers.log")}', file=sys.stderr, flush=True)


class Service:
    def __init__(self, installer, workers=1, cores=None):
        self.installer, self.workers = installer, workers
        # None (CF_V2_CORES empty): pass no --cores, so the evaluator sizes itself (CF_CPUS / CF_MEMORY /
        # its config file, default auto). A count is the deprecated explicit alias, kept for old setups.
        self.cores = cores
        self.sha, self.proc, self.port, self.log_path = None, None, None, None
        self.env = None             # the running service's environment (its state dir and lock paths)
        self.healthy = False
        self.oom_start = None
        self.not_ready = None       # v2.0.1: the running service's "not ready" reason (RAM plan), or None

    def was_oom_killed(self):
        count = cgroups.oom_kills()
        return (self.proc is not None and self.proc.poll() == -signal.SIGKILL
                and self.oom_start is not None and count is not None and count > self.oom_start)

    def _read_health(self, status, body):
        """v2.0.1: 200 healthy; 503 with status "not_ready" is an evaluator ALIVE that waits for RAM (its RAM plan
        cannot execute yet; it re-checks itself): kept, never a start failure. -> True when it answered either."""
        try:
            doc = json.loads(body or b'{}')
        except ValueError:
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        waiting = doc.get('reason') if status == 503 and doc.get('status') == 'not_ready' else doc.get('modes_not_ready')
        if waiting and waiting != self.not_ready:
            print(f'evaluator service {(self.sha or "")[:12]} not ready: {waiting}', file=sys.stderr, flush=True)
        self.not_ready = waiting or None
        self.healthy = status == 200
        return status == 200 or (status == 503 and doc.get('status') == 'not_ready')

    def stop(self):
        self.healthy = False
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()  # The evaluator drains its own children/checkpoints.
                self.proc.wait()
            else:
                report_exit(self.sha or '', self.proc, self.log_path, 'unexpectedly')
                self.reap_orphans(self.proc.pid)
            self.proc = None
            env = self.env or {}
            boxes.reap_unowned(self.installer.root, 'evaluator service stopped', state=env.get('CF_ORCH_SSD'), environ=env)

    @staticmethod
    def reap_orphans(group):
        # After an abrupt parent death, do not leave old broker/workers alongside a new service.
        try:
            os.killpg(group, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.1)
                os.killpg(group, 0)
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def ensure(self, sha):
        if self.sha == sha and self.proc and self.proc.poll() is None:
            status, _, body = self.request('GET', '/health')
            self._read_health(status, body)
            return
        self.stop()
        ready = self.installer.ready(sha)
        if not ready:
            raise InstallError('EVALUATOR_NOT_READY')
        self.sha = sha
        root = self.installer.root
        tree = root / 'evaluators' / sha
        manifest = ready['manifest']
        # Every box the service starts carries this root's label; none of ours may run before it starts.
        env = boxes.with_label(environment(root, sha, self.installer.salt), root)
        self.env = env
        boxes.reap_unowned(root, 'before evaluator service start', state=env.get('CF_ORCH_SSD'), environ=env)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        command = [interpreter(), '-B', str(tree / manifest['service']), '--host', '127.0.0.1',
                   '--port', str(self.port), '--state', env['CF_ORCH_SSD'], '--posix', env['CF_ORCH_POSIX'],
                   '--policy', str(tree / manifest['policy']), '--workers', str(self.workers),
                   '--lanes', str(root / 'lanes')]
        if self.cores is not None:
            command += ['--cores', str(self.cores)]
        if self.installer.allow_runc:
            command.append('--allow-runc')
        # v2.0.1: the service's own output (start-up plan, worker restarts with their exit evidence,
        # tracebacks) goes to service.log beside workers.log in its state directory (owner-only root),
        # not to /dev/null. One previous generation is kept.
        self.log_path = Path(env['CF_ORCH_SSD']) / 'service.log'
        try:
            if self.log_path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(self.log_path, self.log_path.with_name('service.log.1'))
        except FileNotFoundError:
            pass
        self.oom_start = cgroups.oom_kills()
        # No preexec_fn in this threaded loader: reset the inherited protection before exec.
        command = [sys.executable, "-B", "-c",
                   "import os,sys; open('/proc/self/oom_score_adj','w').write('500'); "
                   "os.execv(sys.argv[1],sys.argv[1:])", *command]
        with open(self.log_path, 'ab') as log:
            self.proc = subprocess.Popen(command, cwd=tree, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                report_exit(sha, self.proc, self.log_path, 'during start-up')
                # Signal only this service's process group, never another evaluator/host job.
                self.reap_orphans(self.proc.pid)
                raise InstallError('EVALUATOR_SERVICE_EXITED')
            try:
                status, _, body = self.request('GET', '/health')
                if self._read_health(status, body):        # healthy, or alive and waiting for RAM: kept
                    atomic_json(root / 'loader/active.json', {'sha256': sha})
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        self.stop()
        raise InstallError('EVALUATOR_SERVICE_START_FAILED')

    def request(self, method, path, body=None, content_type=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        try:
            headers = {}
            if body:
                headers = {'Content-Type': content_type, 'Content-Length': str(Path(body).stat().st_size)}
                with open(body, 'rb') as f:
                    connection.request(method, path, body=f, headers=headers)
            else:
                connection.request(method, path)
            response = connection.getresponse()
            data = response.read(16 << 20)
            if response.read(1):
                raise InstallError('EVALUATOR_RESPONSE_TOO_LARGE')
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def submit(self, job):
        boundary = 'cf-' + uuid.uuid4().hex
        with tempfile.NamedTemporaryFile(dir=self.installer.root / 'tmp') as f:
            for key in ('request_id', 'mode', 'submitter', 'chain_commit'):
                if key == 'chain_commit' and not job.get(key):
                    continue          # only sent when the caller sent one (evaluators before BEACON refuse it)
                f.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{job[key]}\r\n'.encode())
            f.write(f'--{boundary}\r\nContent-Disposition: form-data; name="design_zip"; filename="design.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode())
            with open(job['design'], 'rb') as design:
                while data := design.read(1 << 20):
                    f.write(data)
            f.write(f'\r\n--{boundary}--\r\n'.encode())
            f.flush()
            return self.request('POST', '/evaluate', f.name, 'multipart/form-data; boundary=' + boundary)
