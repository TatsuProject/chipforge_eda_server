"""Loopback-only evaluator process, restarted after a crash; one active evaluator."""
import http.client
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import uuid

from .install import environment, interpreter
from .security import InstallError, atomic_json


class Service:
    def __init__(self, installer, workers=1, cores=None):
        self.installer, self.workers = installer, workers
        self.cores = cores or len(os.sched_getaffinity(0))
        self.sha, self.proc, self.port = None, None, None
        self.healthy = False

    def stop(self):
        self.healthy = False
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()  # The evaluator drains its own children/checkpoints.
                self.proc.wait()
            else:
                self.reap_orphans(self.proc.pid)
            self.proc = None

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
            status, _, _ = self.request('GET', '/health')
            self.healthy = status == 200
            return
        self.stop()
        ready = self.installer.ready(sha)
        if not ready:
            raise InstallError('EVALUATOR_NOT_READY')
        self.sha = sha
        root = self.installer.root
        tree = root / 'evaluators' / sha
        manifest = ready['manifest']
        env = environment(root, sha, self.installer.salt)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        command = [interpreter(), '-B', str(tree / manifest['service']), '--host', '127.0.0.1',
                   '--port', str(self.port), '--state', env['CF_ORCH_SSD'], '--posix', env['CF_ORCH_POSIX'],
                   '--policy', str(tree / manifest['policy']), '--workers', str(self.workers),
                   '--cores', str(self.cores), '--lanes', str(root / 'lanes')]
        if self.installer.allow_runc:
            command.append('--allow-runc')
        self.proc = subprocess.Popen(command, cwd=tree, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                # Signal only this service's process group, never another evaluator/host job.
                self.reap_orphans(self.proc.pid)
                raise InstallError('EVALUATOR_SERVICE_EXITED')
            try:
                status, _, _ = self.request('GET', '/health')
                if status == 200:
                    self.healthy = True
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
            for key in ('request_id', 'mode', 'submitter'):
                f.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{job[key]}\r\n'.encode())
            f.write(f'--{boundary}\r\nContent-Disposition: form-data; name="design_zip"; filename="design.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode())
            with open(job['design'], 'rb') as design:
                while data := design.read(1 << 20):
                    f.write(data)
            f.write(f'\r\n--{boundary}--\r\n'.encode())
            f.flush()
            return self.request('POST', '/evaluate', f.name, 'multipart/form-data; boundary=' + boundary)
