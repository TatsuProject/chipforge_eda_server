"""HTTP front end for automatic installation and durable evaluator jobs."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

from .install import interpreter
from .multipart import HTTPError, parse
from .runtime import ID, Runtime
from .security import SHA, PrivateModeError


class Server(ThreadingHTTPServer):
    # Finish already accepted upload handlers before releasing the durable root lock.
    daemon_threads = False
    request_queue_size = 32

    def __init__(self, address, runtime):
        self.runtime = runtime
        self.upload_slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ChipForgeLoader/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def log_message(self, *_):
        pass  # URLs/fields and evaluator output never enter access logs.

    def send(self, status, body, headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        out = {'Content-Type': 'application/json', 'Cache-Control': 'no-store',
               'X-CF-V2-Allow-Unsigned': str(int(self.server.runtime.allow_unsigned)),
               'X-CF-Scope-Backend': 'cfscope', **(headers or {})}
        for key, value in out.items():
            self.send_header(key, str(value))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        runtime = self.server.runtime
        try:
            path = urlsplit(self.path).path
            if path.startswith('/v2/'):
                path = path[3:]
            if post and path in ('/evaluate', '/evaluator', '/admin/install'):
                if not self.server.upload_slots.acquire(blocking=False):
                    raise HTTPError(503, 'UPLOAD_BUSY', 'Retry the same request ID.')
                try:
                    if self.headers.get('Transfer-Encoding'):
                        raise HTTPError(411, 'CONTENT_LENGTH_REQUIRED', 'Chunked uploads are unsupported.')
                    lengths = self.headers.get_all('Content-Length', [])
                    if len(lengths) != 1 or not lengths[0].isdigit():
                        raise HTTPError(411, 'CONTENT_LENGTH_REQUIRED', 'Provide one Content-Length.')
                    length = int(lengths[0])
                    if length > (512 << 20) + (64 << 10):
                        raise HTTPError(413, 'UPLOAD_TOO_LARGE', 'Upload exceeds 512 MiB.')
                    with tempfile.TemporaryDirectory(dir=runtime.root / 'tmp') as d:
                        fields = parse(self.rfile, length, self.headers.get('Content-Type', ''), d, path == '/evaluate')
                        if path == '/evaluate':
                            job = runtime.accept(fields)
                            # New jobs always receive immediate 202, including installing_evaluator.
                            self.job_response(job)
                        else:
                            sha = runtime.archive(fields)
                            runtime.request_install(sha)
                            self.send(202, dict(evaluator_sha256=sha, install_url='/v2/evaluator/' + sha),
                                      {'Location': '/v2/evaluator/' + sha, 'Retry-After': '5'})
                finally:
                    self.server.upload_slots.release()
            elif not post and path == '/health':
                health = runtime.health()
                self.send(200 if health['ready'] else 503, health)
            elif not post and path == '/evaluator':
                with runtime.db() as db:
                    shas = [r[0] for r in db.execute('SELECT DISTINCT sha FROM installs ORDER BY sha')]
                self.send(200, dict(evaluators=[self.install_body(s) for s in shas], active=runtime.service.sha))
            elif not post and path.startswith('/evaluator/'):
                sha = path.removeprefix('/evaluator/')
                if not SHA.fullmatch(sha) or not runtime.install_status(sha):
                    raise HTTPError(404, 'NOT_FOUND', 'Unknown evaluator.')
                self.send(200, self.install_body(sha))
            elif not post and path.startswith('/status/'):
                rid = path.removeprefix('/status/')
                job = runtime.job(rid) if ID.fullmatch(rid) else None
                if not job:
                    raise HTTPError(404, 'NOT_FOUND', 'Unknown request ID.')
                self.job_response(job)
            else:
                raise HTTPError(404, 'NOT_FOUND', 'Unknown endpoint.')
        except HTTPError as exc:
            self.close_connection = True  # Unconsumed oversized/bad bodies cannot become another request.
            self.send(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, ValueError, UnicodeError):
            self.close_connection = True
            self.send(400, {'error': {'code': 'BAD_REQUEST', 'message': 'Invalid or timed out request.'}})
        except Exception:
            self.close_connection = True
            self.send(503, {'error': {'code': 'LOADER_UNAVAILABLE', 'message': 'Retry the same request ID.'}})

    def install_body(self, sha):
        runtime = self.server.runtime
        row = runtime.install_status(sha)
        return dict(evaluator_sha256=sha, phase=row['phase'], attempt=row['attempt'], error=row['error'],
                    unsigned=bool(row['unsigned']), allow_unsigned=runtime.allow_unsigned)

    def job_response(self, job):
        runtime = self.server.runtime
        installed = runtime.install_status(job['sha'], job['attempt'])
        headers = {'X-Request-ID': job['request_id'], 'X-CF-Evaluator-Sha256': job['sha'],
                   'X-CF-Evaluator-Unsigned': str(int(bool(installed and installed['unsigned']))),
                   'X-CF-Phase': job['phase'], 'Location': '/status/' + job['request_id']}
        if job['response'] is not None:
            headers['Content-Type'] = job['response_type']
            return self.send(200, job['response'], headers)
        headers['Retry-After'] = '5'
        self.send(202, dict(request_id=job['request_id'], evaluator_sha256=job['sha'], phase=job['phase'],
                            status_url='/v2/status/' + job['request_id'], mode=job['mode']), headers)


def parse_cores(value, available):
    """CF_V2_CORES is a count of CPUs to use, not a CPU list or range. Empty -> None: the evaluator
    sizes itself (CF_CPUS / CF_MEMORY / its config file; default auto = every CPU it may use)."""
    text = (value or '').strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit() and int(text) >= 1):
        raise ValueError(f'CF_V2_CORES must be a positive integer (a number of CPUs, e.g. 8), got {value!r}; '
                         'to restrict the loader to specific CPUs use a compose override "cpuset:" instead')
    if int(text) > available:
        raise ValueError(f'CF_V2_CORES={text} exceeds the {available} CPUs available to this container')
    return int(text)


CONFIG_EXAMPLE = '{"resources": {"cpus": "auto", "memory": "auto"}, "waits": {"step_bound_factor": 8}}'

# Run INSIDE the installed evaluator, with its own interpreter: its resources.read_config_doc() is the
# one list of accepted top-level sections (resources, waits, simulation, ... as that version defines
# them), and its own loaders validate the values. The loader keeps no second copy that could drift.
EVALUATOR_CONFIG_CHECK = """
import sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/v2/pipeline']
from orchestrator import resources
resources.read_config_doc()
resources.load_config_file()
# the values, as the evaluator resolves them at start-up (each guarded: older evaluators lack some)
for name in ('process_budget', 'compile_workers_setting'):
    if hasattr(resources, name):
        getattr(resources, name)()
for name in ('waits', 'simspeed'):
    try:
        module = __import__('orchestrator.' + name, fromlist=[name])
    except ImportError:
        continue
    module.load()
"""


def check_evaluator_config(root, environ, tree=None, python=None):
    """Fail at start-up, naming the file, when the evaluator config (CF_EVALUATOR_CONFIG, else
    <CF_V2_ROOT>/evaluator.json) is not what the evaluator reads: otherwise every install fails its
    preflight, or the service exits, as an unscored system fault. The loader itself checks only that
    the file exists (when named) and is a JSON object without the retired flat {"cpus": ..} form; the
    sections and values are validated by the installed evaluator `tree` (the newest ready one) when
    there is one. Without one the first install's preflight validates it. -> path or None (absent)."""
    explicit = (environ.get('CF_EVALUATOR_CONFIG') or '').strip()
    path = Path(explicit) if explicit else Path(root) / 'evaluator.json'
    if not path.is_file():
        if explicit:
            raise ValueError(f'CF_EVALUATOR_CONFIG={explicit}: no such file inside the container '
                             '(put it under CF_V2_ROOT, which is mounted at the same path)')
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f'evaluator config {path}: not readable JSON ({exc})') from None
    if not isinstance(doc, dict):
        raise ValueError(f'evaluator config {path}: must be a JSON object; e.g. {CONFIG_EXAMPLE}')
    if set(doc) & {'cpus', 'memory'}:
        raise ValueError(f'evaluator config {path}: the old flat {{"cpus": ..}} form is no longer read; '
                         f'nest them under "resources", e.g. {CONFIG_EXAMPLE}')
    if tree is not None and (Path(tree) / 'v2/pipeline/orchestrator/resources.py').is_file():
        env = {k: v for k, v in environ.items() if not k.startswith('PYTHON')}
        env.update(CF_V2_ROOT=str(root), CF_EVALUATOR_CONFIG=str(path), PYTHONDONTWRITEBYTECODE='1')
        proc = subprocess.run([python or sys.executable, '-I', '-B', '-c', EVALUATOR_CONFIG_CHECK, str(tree)],
                              env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if proc.returncode:
            last = (proc.stderr.strip().splitlines() or ['exit %d' % proc.returncode])[-1]
            raise ValueError(f'evaluator config {path} is refused by the installed evaluator '
                             f'{Path(tree).name[:12]}: {last.split(": ", 1)[-1][:600]}')
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=18080)
    args = ap.parse_args()
    root = Path(os.environ.get('CF_V2_ROOT') or '/var/lib/chipforge-v2')
    if not root.is_absolute() or root == Path('/'):
        ap.error('CF_V2_ROOT must be an absolute data directory')
    salt = Path(os.environ.get('CF_EVALUATOR_SALT_FILE', str(root.parent / (root.name + '-secrets') / 'evaluator-salt.bin')))
    public_key = os.environ.get('CF_V2_EVALUATOR_PUBLIC_KEY', '')
    if key_file := os.environ.get('CF_V2_EVALUATOR_PUBLIC_KEY_FILE'):
        public_key = Path(key_file).read_text().strip()
    modes = tuple(x for x in os.environ.get('CF_V2_ALLOWED_MODES', 'T2').replace(',', ' ').split() if x)
    workers = int(os.environ.get('CF_V2_WORKERS') or '1')
    try:
        cores = parse_cores(os.environ.get('CF_V2_CORES'), len(os.sched_getaffinity(0)))
    except ValueError as exc:
        ap.error(str(exc))
    if not modes or set(modes) - {'DEV', 'T1', 'T2'} or not 1 <= workers <= 2 or (
            cores is not None and not 1 <= cores <= len(os.sched_getaffinity(0))):
        ap.error('invalid modes, workers or CPU allocation')
    try:
        check_evaluator_config(root, os.environ)             # file shape before anything starts
    except ValueError as exc:
        ap.error(str(exc))
    min_disk = os.environ.get('CF_V2_PREFLIGHT_MIN_DISK_GIB', '').strip()
    if min_disk and not (min_disk.isdigit() and 1 <= int(min_disk) <= 100000):
        ap.error('CF_V2_PREFLIGHT_MIN_DISK_GIB must be empty or a whole number of GiB')
    try:
        runtime = Runtime(root, salt, public_key, os.environ.get('CF_V2_ALLOW_UNSIGNED') == '1', modes,
                          os.environ.get('CF_V2_ALLOW_RUNC') == '1', workers, cores,
                          int(min_disk) if min_disk else None)
    except PrivateModeError as exc:
        ap.error(str(exc))
    try:                                                      # sections and values: the evaluator's own rules
        sha = runtime.latest_ready()
        check_evaluator_config(root, os.environ, root / 'evaluators' / sha if sha else None, interpreter())
    except ValueError as exc:
        runtime.close()
        ap.error(str(exc))
    server = Server((args.host, args.port), runtime)
    runtime.start()

    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        runtime.close()


if __name__ == '__main__':
    main()
