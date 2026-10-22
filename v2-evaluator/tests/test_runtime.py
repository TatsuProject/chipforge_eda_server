from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import threading
import time
import uuid

import pytest

from loader.runtime import Runtime
from loader.server import Server
from fixtures import RAW, build_zip, keypair, sign


def wait_until(fn, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value: return value
        time.sleep(0.025)
    raise AssertionError('timed out waiting for expected state')


def multipart(fields):
    boundary = 'fixture-' + uuid.uuid4().hex
    pieces = []
    for name, value in fields.items():
        if isinstance(value, Path): value = value.read_bytes()
        if isinstance(value, str): value = value.encode()
        pieces += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value, b'\r\n']
    pieces += [f'--{boundary}--\r\n'.encode()]
    return b''.join(pieces), 'multipart/form-data; boundary=' + boundary


@contextmanager
def running(tmp_path, **kwargs):
    runtime = Runtime(tmp_path / 'root', tmp_path / 'secrets/salt', **kwargs)
    runtime.start()
    server = Server(('127.0.0.1', 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def request(method, path, fields=None, raw=None, headers=None):
        body, h = None, dict(headers or {})
        if fields is not None:
            body, h['Content-Type'] = multipart(fields)
        elif raw is not None:
            body = raw
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()
    try:
        yield runtime, request
    finally:
        server.shutdown(); server.server_close(); thread.join(); runtime.close()


def terminal(request, rid):
    result = request('GET', '/v2/status/' + rid)
    return result if result[0] == 200 else None


def test_automatic_install_default_mode_passthrough_and_idempotence(tmp_path):
    archive = tmp_path / 'e.zip'
    sha = build_zip(archive, preflight_delay=0.3)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        assert request('GET', '/health')[0] == 503
        status, headers, body = request('POST', '/v2/evaluate', dict(design_zip=b'dummy', evaluator_zip=archive, request_id='first'))
        assert status == 202 and json.loads(body)['phase'] == 'installing_evaluator'
        assert headers['Location'] == '/status/first'
        assert headers['X-CF-V2-Allow-Unsigned'] == '1'
        status, headers, body = wait_until(lambda: terminal(request, 'first'))
        assert body == RAW and headers['X-CF-Evaluator-Sha256'] == sha
        assert headers['X-CF-Evaluator-Unsigned'] == '1' and headers['X-CF-Phase'] == 'done'
        assert request('POST', '/evaluate', dict(design_zip=b'dummy', evaluator_sha256=sha, request_id='first'))[2] == RAW
        assert request('POST', '/evaluate', dict(design_zip=b'different', evaluator_sha256=sha, request_id='first'))[0] == 409
        assert request('GET', '/evaluator/' + sha)[0] == 200
        assert json.loads(request('GET', '/evaluator')[2])['active'] == sha
        assert request('GET', '/status/unknown')[0] == 404
        assert request('GET', '/health')[0] == 200
        # The salt's bytes and path never occur in any response.
        assert runtime.installer.salt.read_bytes() not in body
        assert str(runtime.installer.salt).encode() not in body


def test_signed_install_and_wrong_signature_is_system_error(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive)
    key, public = keypair(tmp_path)
    signature = sign(archive, key)
    with running(tmp_path, public_key=public) as (_, request):
        status, _, body = request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, evaluator_sig=b'invalid', request_id='bad'))
        assert status == 202
        _, headers, body = wait_until(lambda: terminal(request, 'bad'))
        failure = json.loads(body)
        assert failure['status'] == 'ERROR' and failure['score'] is None
        assert failure['problems'][0]['fault'] == 'system'
        assert failure['problems'][0]['code'] == 'EVALUATOR_SIGNATURE_INVALID'
        assert headers['X-CF-V2-Allow-Unsigned'] == '0'
        # Retry same ID with corrected signature installs again and reaches the unchanged stub response.
        assert request('POST', '/evaluate', dict(design_zip=b'd', evaluator_sha256=sha, evaluator_sig=signature, request_id='bad'))[0] == 202
        assert wait_until(lambda: terminal(request, 'bad'))[2] == RAW


@pytest.mark.parametrize('fault', ['unsigned', 'hash', 'preflight'])
def test_install_faults_unscored_and_next_request_retries(tmp_path, fault):
    archive = tmp_path / 'e.zip'
    sha = build_zip(archive, fail_preflight=fault == 'preflight')
    fields = dict(design_zip=b'd', evaluator_zip=archive, request_id='bad')
    if fault == 'hash': fields['evaluator_sha256'] = '0'*64
    with running(tmp_path, allow_unsigned=fault != 'unsigned') as (runtime, request):
        assert request('POST', '/evaluate', fields)[0] == 202
        _, _, raw = wait_until(lambda: terminal(request, 'bad'))
        body = json.loads(raw)
        assert body['status'] == 'ERROR' and body['score'] is None
        assert body['problems'][0]['fault'] == 'system' and body['problems'][0]['retryable'] is True
        expected = {'unsigned':'EVALUATOR_SIGNATURE_REQUIRED', 'hash':'EVALUATOR_HASH_MISMATCH', 'preflight':'EVALUATOR_PREFLIGHT_FAILED'}[fault]
        assert body['problems'][0]['code'] == expected
        if fault == 'preflight':
            (runtime.root / 'permit-preflight').touch()
            fields['request_id'] = 'retry'
            assert request('POST', '/evaluate', fields)[0] == 202
            assert wait_until(lambda: terminal(request, 'retry'))[2] == RAW
            assert runtime.install_status(sha)['attempt'] == 2


def test_concurrent_new_requests_share_install(tmp_path):
    archive = tmp_path / 'e.zip'; build_zip(archive, preflight_delay=0.4, delay=0.1)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        def submit(i):
            return request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id=f'r{i}'))[0]
        # Upload bound is separate from the install lock: retry admission if the two disk streams are busy.
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(submit, range(2))) == [202, 202]
        assert wait_until(lambda: terminal(request, 'r0'))[2] == RAW
        assert wait_until(lambda: terminal(request, 'r1'))[2] == RAW
        assert (runtime.root / 'preflight-count').read_text() == '1'


def test_optional_prewarm_and_bad_requests(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        status, _, body = request('POST', '/v2/evaluator', dict(evaluator_zip=archive))
        assert status == 202 and json.loads(body)['install_url'] == '/v2/evaluator/' + sha
        wait_until(lambda: runtime.install_status(sha)['phase'] == 'ready')
        for fields, code in [
            (dict(design_zip=b'd', request_id='r'), 400),
            (dict(design_zip=b'd', request_id='../escape', evaluator_sha256=sha), 400),
            (dict(design_zip=b'd', request_id='r', evaluator_sha256='X'*64), 400),
            (dict(design_zip=b'd', request_id='r', evaluator_sha256='0'*64), 404),
            (dict(design_zip=b'd', request_id='r', evaluator_sha256=sha, mode='T1'), 400),
            (dict(request_id='r', evaluator_sha256=sha), 400),
        ]:
            assert request('POST', '/evaluate', fields)[0] == code
        assert request('GET', '/evaluator/nope')[0] == 404
        assert request('POST', '/evaluate', raw=b'x', headers={'Content-Type':'text/plain'})[0] == 415


@pytest.mark.parametrize('crash_after_forward', [False, True])
def test_service_restart_and_durable_loader_restart(tmp_path, crash_after_forward):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=1.5)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id='restart'))
        wait_until(lambda: runtime.service.proc and runtime.service.proc.poll() is None)
        if crash_after_forward:
            wait_until(lambda: runtime.job('restart')['forwarded'])
        old_pid = runtime.service.proc.pid
        os.kill(old_pid, signal.SIGKILL)
        assert wait_until(lambda: terminal(request, 'restart'))[2] == RAW
        assert runtime.service.proc.pid != old_pid
        salt = runtime.installer.salt.read_bytes()
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        assert terminal(request, 'restart')[2] == RAW
        assert runtime.installer.salt.read_bytes() == salt
        assert request('POST', '/evaluate', dict(design_zip=b'd2', evaluator_sha256=sha, request_id='cached'))[0] == 202
        assert wait_until(lambda: terminal(request, 'cached'))[2] == RAW
        assert (runtime.root / 'preflight-count').read_text() == '1'


def test_progress_phases_and_prewarm_health(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=1.5, preflight_delay=0.3)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id='phases'))
        phases = set()
        def poll():
            status, headers, body = request('GET', '/status/phases')
            phases.add(headers['X-CF-Phase'])
            return status == 200
        wait_until(poll)
        assert phases == {'installing_evaluator', 'queued', 'running', 'done'}
    # Installed evaluator starts again even with no pending jobs.
    with running(tmp_path, allow_unsigned=True) as (_, request):
        wait_until(lambda: request('GET', '/health')[0] == 200)


def test_loader_restart_with_inflight_job_and_single_root_lock(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=1.5)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id='durable'))
        wait_until(lambda: runtime.job('durable')['forwarded'])
        with pytest.raises(BlockingIOError):
            Runtime(runtime.root, runtime.installer.salt, allow_unsigned=True)
    with running(tmp_path, allow_unsigned=True) as (_, request):
        assert wait_until(lambda: terminal(request, 'durable'))[2] == RAW


def test_failed_concurrent_install_attempt_not_retried_by_waiters(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, fail_preflight=True, preflight_delay=0.4)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        for rid in ('a', 'b'):
            assert request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id=rid))[0] == 202
        for rid in ('a', 'b'):
            response = json.loads(wait_until(lambda: terminal(request, rid))[2])
            assert response['problems'][0]['fault'] == 'system'
        assert runtime.install_status(sha)['attempt'] == 1
        assert (runtime.root / 'preflight-count').read_text() == '1'


def test_tool_provision_failure_is_system_error_not_stuck_install(tmp_path, monkeypatch):
    import loader.runtime as module
    def unavailable(_):
        raise OSError('synthetic infrastructure failure with private diagnostic')
    monkeypatch.setattr(module, 'provision_tools', unavailable)
    archive = tmp_path / 'e.zip'; build_zip(archive)
    with running(tmp_path, allow_unsigned=True) as (_, request):
        assert request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id='infra'))[0] == 202
        _, _, raw = wait_until(lambda: terminal(request, 'infra'))
        body = json.loads(raw)
        assert body['status'] == 'ERROR' and body['score'] is None
        assert body['problems'][0]['fault'] == 'system'
        assert b'private diagnostic' not in raw


def test_terminal_evaluator_row_without_response_errors_and_releases_the_loader(tmp_path):
    # Lab incident: the evaluator marked its queue row done with no response and kept answering 202
    # "done"; the loader kept the request "forwarded" for 29 min, pinned to that evaluator, and every
    # newer request waited behind it. Now the caller gets a terminal ERROR and the next request runs.
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=4)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'stuck', evaluator_zip=archive, request_id='stuck'))
        wait_until(lambda: runtime.job('stuck')['forwarded'])
        request('POST', '/evaluate', dict(design_zip=b'next', evaluator_sha256=sha, request_id='next'))
        assert runtime.job('next')['forwarded'] == 0                # held behind the active request
        (runtime.root / 'state' / sha / 'stuck.done-without-response').touch()
        status, headers, body = wait_until(lambda: terminal(request, 'stuck'))
        doc = json.loads(body)
        assert (doc['status'], doc['score'], doc['reward_eligible']) == ('ERROR', None, False)
        assert doc['problems'][0]['code'] == 'EVALUATOR_RESULT_MISSING' and doc['problems'][0]['fault'] == 'system'
        assert headers['X-CF-Phase'] == 'done'
        assert wait_until(lambda: terminal(request, 'next'))[2] == RAW   # the loader is released
        assert terminal(request, 'stuck')[2] == body                    # and the error is durable


def test_operator_cancel_in_the_evaluator_or_the_loader(tmp_path):
    from loader.cancel import cancel
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='running'))
        wait_until(lambda: runtime.job('running')['forwarded'])
        request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='waiting'))
        salt = runtime.installer.salt
        # Not handed to the evaluator yet: the loader finishes it itself.
        code, out = cancel(runtime.root, salt, 'waiting', 'host maintenance')
        assert (code, out['by'], out['code']) == (0, 'loader', 'EVALUATION_CANCELLED')
        doc = json.loads(terminal(request, 'waiting')[2])
        assert doc['status'] == 'ERROR' and doc['problems'][0]['code'] == 'EVALUATION_CANCELLED'
        assert 'host maintenance' in doc['problems'][0]['detail']
        # Running in the evaluator: the evaluator cancels it; the loader picks the ERROR up and is free.
        code, out = cancel(runtime.root, salt, 'running', 'host maintenance')
        assert (code, out['by'], out['cancelled']) == (0, 'evaluator', True)
        status, _, body = wait_until(lambda: terminal(request, 'running'))
        assert json.loads(body)['problems'][0]['code'] == 'EVALUATION_CANCELLED'
        assert cancel(runtime.root, salt, 'running', 'x')[1]['reason'].startswith('already finished')
        assert cancel(runtime.root, salt, 'no-such-request', 'x')[0] == 3
        request('POST', '/evaluate', dict(design_zip=b'c', evaluator_sha256=sha, request_id='after'))
        wait_until(lambda: runtime.job('after')['forwarded'])         # the loader is not blocked


def test_chain_commit_is_stored_forwarded_and_part_of_idempotency(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=0.3)
    cc = {'schema': 'cf-chain-commit/1', 'netuid': 108, 'block_number': 7, 'hotkey': '5F'}
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        fields = dict(design_zip=b'd', evaluator_zip=archive, request_id='chained', chain_commit=json.dumps(cc, indent=2))
        assert request('POST', '/evaluate', fields)[0] == 202
        wait_until(lambda: terminal(request, 'chained'))
        sent = json.loads((runtime.root / 'state' / sha / 'chained.json').read_text())['chain_commit']
        assert json.loads(sent) == cc and sent == json.dumps(cc, sort_keys=True, separators=(',', ':'))
        same = dict(fields, chain_commit=json.dumps(cc), evaluator_zip=None)
        del same['evaluator_zip']; same['evaluator_sha256'] = sha
        assert request('POST', '/evaluate', same)[0] == 200                       # same object, any spacing
        assert request('POST', '/evaluate', dict(same, chain_commit=json.dumps(dict(cc, block_number=8))))[0] == 409
        assert request('POST', '/evaluate', dict(same, request_id='bad', chain_commit='not json'))[0] == 400
        assert request('POST', '/evaluate', dict(same, request_id='big', chain_commit='{"x":"' + 'a' * 2100 + '"}'))[0] in (400, 413)
        request('POST', '/evaluate', dict(design_zip=b'd2', evaluator_sha256=sha, request_id='plain'))
        wait_until(lambda: terminal(request, 'plain'))
        assert json.loads((runtime.root / 'state' / sha / 'plain.json').read_text())['chain_commit'] is None
        # An evaluator refusal names its public code to the caller.
        request('POST', '/evaluate', dict(design_zip=b'refused-by-evaluator', evaluator_sha256=sha, request_id='refused'))
        doc = json.loads(wait_until(lambda: terminal(request, 'refused'))[2])
        assert doc['status'] == 'ERROR' and doc['problems'][0]['code'] == 'EVALUATOR_SERVICE_REJECTED'
        assert 'CHAIN_COMMIT_REQUIRED' in doc['problems'][0]['detail']


def test_evaluator_config_shape_is_checked_at_startup(tmp_path):
    from loader.server import check_evaluator_config
    root = tmp_path / 'root'; root.mkdir()
    assert check_evaluator_config(root, {}) is None                     # absent: every default (auto)
    cfg = root / 'evaluator.json'
    cfg.write_text(json.dumps({'resources': {'cpus': 36, 'memory': 'auto'}, 'waits': {'step_idle_s': 7200}}))
    assert check_evaluator_config(root, {}) == cfg
    cfg.write_text(json.dumps({'cpus': 36, 'memory': 'auto'}))         # the old flat form (lab r39)
    with pytest.raises(ValueError, match='old flat'):
        check_evaluator_config(root, {})
    with pytest.raises(ValueError, match='no such file'):
        check_evaluator_config(root, {'CF_EVALUATOR_CONFIG': str(tmp_path / 'missing.json')})
    other = tmp_path / 'other.json'; other.write_text('{"resources": {"cpu": 4}}')
    with pytest.raises(ValueError, match='only "cpus" and "memory"'):
        check_evaluator_config(root, {'CF_EVALUATOR_CONFIG': str(other)})
    import subprocess, sys
    env = dict(os.environ, CF_V2_ROOT=str(root), CF_V2_CORES='')
    proc = subprocess.run([sys.executable, '-m', 'loader.server'], cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and 'old flat' in proc.stderr


def test_empty_cf_v2_cores_leaves_sizing_to_the_evaluator(tmp_path):
    archive = tmp_path / 'e.zip'; build_zip(archive, delay=0.2)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_zip=archive, request_id='auto'))
        wait_until(lambda: terminal(request, 'auto'))
        cmd = Path(f'/proc/{runtime.service.proc.pid}/cmdline').read_bytes().split(b'\0')
        assert b'--cores' not in cmd
    with running(tmp_path, allow_unsigned=True, cores=2) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'd2', evaluator_zip=archive, request_id='two'))
        wait_until(lambda: terminal(request, 'two'))
        cmd = Path(f'/proc/{runtime.service.proc.pid}/cmdline').read_bytes().split(b'\0')
        assert cmd[cmd.index(b'--cores') + 1] == b'2'


def test_cf_v2_cores_validation():
    from loader.server import parse_cores
    assert parse_cores(None, 6) is None and parse_cores('', 6) is None and parse_cores(' 4 ', 6) == 4
    for bad in ('16-31', '0', '-1', 'abc', '1.5', '2,3'):
        with pytest.raises(ValueError, match='CF_V2_CORES must be a positive integer'):
            parse_cores(bad, 64)
    with pytest.raises(ValueError, match='exceeds'):
        parse_cores('9', 8)


def test_cf_v2_cores_range_fails_fast_with_clear_message(tmp_path):
    import subprocess, sys
    env = dict(os.environ, CF_V2_ROOT=str(tmp_path / 'root'), CF_V2_CORES='16-31')
    proc = subprocess.run([sys.executable, '-m', 'loader.server'], cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and 'CF_V2_CORES must be a positive integer' in proc.stderr
