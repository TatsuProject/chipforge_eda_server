"""CF-FG-001: a public terminal result must never leave recoverable backend ownership."""
import json
from types import SimpleNamespace

import pytest

from loader.runtime import Runtime, system_error
from loader.security import InstallError
from loader.server import Handler


def seeded(tmp_path):
    runtime = Runtime(tmp_path / 'root', tmp_path / 'salt', allow_unsigned=True)
    with runtime.db() as db:
        db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,forwarded) "
                   "VALUES('orphan',?,1,'T2','miner','hash','unused','running',0,1)", ('a' * 64,))
    return runtime


def test_terminal_is_hidden_until_backend_fence_and_drain_commit(tmp_path, monkeypatch):
    import loader.runtime as module
    runtime = seeded(tmp_path)
    calls = []
    def unavailable(root, salt, job, body):
        assert runtime.job('orphan')['phase'] == 'fencing'
        calls.append(json.loads(body)['problems'][0]['code'])
        raise InstallError('EVALUATOR_FENCE_FAILED')
    monkeypatch.setattr(module, 'fence', unavailable)
    try:
        with pytest.raises(InstallError):
            runtime.fail(runtime.job('orphan'), 'EVALUATOR_SERVICE_EXITED')
        job = runtime.job('orphan')
        sent = []
        Handler.job_response(SimpleNamespace(server=SimpleNamespace(runtime=runtime),
                                            send=lambda *a: sent.append(a)), job)
        assert sent[0][0] == 202
        assert job['phase'] == 'fencing'
        with pytest.raises(InstallError):
            runtime.fail(job, 'LOADER_INFRASTRUCTURE_UNAVAILABLE')
        assert runtime.job('orphan')['response'] == job['response']
        monkeypatch.setattr(module, 'fence', lambda *args: calls.append('drained'))
        runtime.finish(job, job['response'], job['response_type'], job['error'])
        assert calls == ['EVALUATOR_SERVICE_EXITED', 'EVALUATOR_SERVICE_EXITED', 'drained']
        assert runtime.job('orphan')['phase'] == 'done'
        assert runtime.job('orphan')['error'] == 'EVALUATOR_SERVICE_EXITED'
    finally:
        runtime.close()


def test_cancel_terminal_transport_still_drains_backend_and_preserves_result(tmp_path, monkeypatch):
    import loader.cancel as module
    runtime = seeded(tmp_path)
    body = system_error(runtime.job('orphan'), 'EVALUATOR_SERVICE_EXITED')
    with runtime.db() as db:
        db.execute("UPDATE jobs SET phase='done',response=?,error='EVALUATOR_SERVICE_EXITED'", (body,))
    before = runtime.job('orphan')
    called = []
    monkeypatch.setattr(module, 'fence', lambda *args: called.append(args[2]['request_id']))
    try:
        code, out = module.cancel(runtime.root, runtime.installer.salt, 'orphan', 'drain orphan')
        assert code == 0 and out['drained'] is True
        assert called == ['orphan']
        assert runtime.job('orphan') == before
    finally:
        runtime.close()


def test_recovery_drains_intent_before_service_restart(tmp_path, monkeypatch):
    import loader.runtime as module
    runtime = seeded(tmp_path)
    body = system_error(runtime.job('orphan'), 'EVALUATOR_SERVICE_EXITED')
    with runtime.db() as db:
        db.execute("UPDATE jobs SET phase='fencing',response=?,response_type='application/json',error='EVALUATOR_SERVICE_EXITED'", (body,))
    runtime.close()
    runtime = Runtime(tmp_path / 'root', tmp_path / 'salt', allow_unsigned=True)
    calls = []
    def drain(*args):
        calls.append('drain')
        runtime.stop_event.set()
    monkeypatch.setattr(module, 'fence', drain)
    monkeypatch.setattr(runtime.service, 'ensure', lambda sha: pytest.fail('service restarted before fence'))
    runtime.tools_ready = True
    try:
        runtime.dispatch()
        assert calls == ['drain'] and runtime.job('orphan')['phase'] == 'done'
    finally:
        runtime.close()


def test_submit_and_terminal_cancel_are_serialized(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import loader.cancel as module
    runtime = seeded(tmp_path)
    with runtime.db() as db:
        db.execute("UPDATE jobs SET phase='queued',forwarded=0")
    entered, release = threading.Event(), threading.Event()
    def submit(job):
        entered.set()
        assert release.wait(10)
        return 202, {}, b'{"state":"running"}'
    monkeypatch.setattr(runtime.service, 'ensure', lambda sha: None)
    monkeypatch.setattr(runtime.service, 'request', lambda *a: (404, {}, b'{}'))
    monkeypatch.setattr(runtime.service, 'submit', submit)
    monkeypatch.setattr(module, 'fence', lambda *a: {'fenced': True, 'drained': True})
    try:
        with ThreadPoolExecutor(2) as pool:
            ticking = pool.submit(runtime.tick, runtime.job('orphan'))
            assert entered.wait(10)
            cancelling = pool.submit(module.cancel, runtime.root, runtime.installer.salt, 'orphan', 'race')
            assert not cancelling.done()
            release.set()
            ticking.result(timeout=10)
            assert cancelling.result(timeout=10)[1]['drained']
        assert runtime.job('orphan')['phase'] == 'done'
        assert runtime.job('orphan')['error'] == 'EVALUATION_CANCELLED'
    finally:
        release.set()
        runtime.close()


def test_cancel_that_loses_to_backend_completion_keeps_the_real_result(tmp_path, monkeypatch):
    import loader.cancel as module
    runtime = seeded(tmp_path)
    real = {'request_id': 'orphan', 'status': 'PASSED', 'score': {'score': 1}}
    monkeypatch.setattr(module, 'fence', lambda *args: {'completed_response': real, 'fenced': True, 'drained': True})
    try:
        code, out = module.cancel(runtime.root, runtime.installer.salt, 'orphan', 'race')
        assert code == 0 and not out['cancelled'] and out['drained']
        assert json.loads(runtime.job('orphan')['response']) == real
        assert runtime.job('orphan')['error'] is None
    finally:
        runtime.close()
