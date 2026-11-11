"""v2.0.6 loader ops (cf-eval-ops/1): deadlines, HTTP cancel and jobs, stage progress, health, housekeeping."""
import json
import os
import sqlite3
import time

import pytest

from loader import runtime as runtime_module
from loader.runtime import Runtime, deadline_of, system_error, timeout_error
from loader.multipart import HTTPError
from fixtures import RAW, build_zip
from test_runtime import running, terminal, wait_until


def problem(body):
    return json.loads(body)['problems'][0]


def stub_state(runtime, sha, rid):
    p = runtime.root / 'state' / sha / (rid + '.json')
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------------ deadline intake
def test_deadline_sources_validation_and_clamp(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.delenv('CF_V2_MAX_JOB_SECONDS', raising=False)
    assert deadline_of({}, {}, 'r', now, now) is None
    assert deadline_of({'deadline_unix': '1800000600'}, {}, 'r', now, now) == now + 600
    assert deadline_of({'time_limit_s': '300.5'}, {}, 'r', now - 100, now) == now + 200.5
    both = {'deadline_unix': str(int(now + 900)), 'time_limit_s': '600'}
    assert deadline_of(both, {'X-CF-Deadline': str(now + 400)}, 'r', now, now) == now + 400   # earliest wins
    # the header only for this request (or no request id named), and silently ignored when unusable
    assert deadline_of({}, {'X-CF-Deadline': str(now + 50), 'X-CF-Request-Id': 'other'}, 'r', now, now) is None
    assert deadline_of({}, {'X-CF-Deadline': str(now + 50), 'X-CF-Request-Id': 'r'}, 'r', now, now) == now + 50
    for junk in ('soon', 'nan', '-5', str(now + 8 * 86400), str(now - 2 * 86400), '1e9'):
        assert deadline_of({}, {'X-CF-Deadline': junk}, 'r', now, now) is None
    # the form fields are an explicit API: malformed is a refusal
    for name, value in (('deadline_unix', 'x'), ('deadline_unix', '0'), ('deadline_unix', str(now + 8 * 86400)),
                        ('time_limit_s', '-1'), ('time_limit_s', 'inf'), ('time_limit_s', str(8 * 86400)),
                        ('deadline_unix', '1' * 33)):
        with pytest.raises(HTTPError) as e:
            deadline_of({name: value}, {}, 'r', now, now)
        assert e.value.code == 'INVALID_DEADLINE'
    # a past deadline is valid (the request times out unstarted); never more than the job cap ahead
    assert deadline_of({'deadline_unix': str(now - 10)}, {}, 'r', now, now) == now - 10
    assert deadline_of({'time_limit_s': str(86400)}, {}, 'r', now, now) == now + 27600
    monkeypatch.setenv('CF_V2_MAX_JOB_SECONDS', '100')
    assert deadline_of({'time_limit_s': '500'}, {}, 'r', now, now) == now + 100


def test_deadline_is_not_identity_and_only_tightens(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        far = time.time() + 3000
        fields = dict(design_zip=b'a', evaluator_zip=archive, request_id='r1')
        assert request('POST', '/evaluate', dict(fields, deadline_unix=f'{far:.3f}'))[0] == 202
        assert runtime.job('r1')['deadline'] == pytest.approx(far, abs=0.001)
        later = dict(design_zip=b'a', evaluator_sha256=sha, request_id='r1', deadline_unix=f'{far + 500:.0f}')
        assert request('POST', '/evaluate', later)[0] == 202                      # no 409, no change
        assert runtime.job('r1')['deadline'] == pytest.approx(far, abs=0.001)
        assert request('POST', '/evaluate', dict(design_zip=b'a', evaluator_sha256=sha, request_id='r1'))[0] == 202
        assert runtime.job('r1')['deadline'] == pytest.approx(far, abs=0.001)
        tighter = dict(design_zip=b'a', evaluator_sha256=sha, request_id='r1')
        assert request('POST', '/evaluate', tighter, headers={'X-CF-Deadline': f'{far - 1000:.0f}',
                                                             'X-CF-Request-Id': 'r1'})[0] == 202
        assert runtime.job('r1')['deadline'] == pytest.approx(far - 1000, abs=1)
        bad = request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='r2', time_limit_s='abc'))
        assert bad[0] == 400 and json.loads(bad[2])['error']['code'] == 'INVALID_DEADLINE'
        assert runtime.job('r2') is None
        # different bytes under the same ID is still a conflict, deadline or not
        assert request('POST', '/evaluate', dict(design_zip=b'z', evaluator_sha256=sha, request_id='r1',
                                                 time_limit_s='10'))[0] == 409


# ------------------------------------------------------------------ enforcement in the loader
def test_past_deadline_never_starts_and_reports_timeout(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        status, headers, _ = request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='late',
                                                                deadline_unix=f'{time.time() - 5:.0f}'))
        assert status == 202
        status, _, body = wait_until(lambda: terminal(request, 'late'))
        doc = json.loads(body)
        p = doc['problems'][0]
        assert doc['status'] == 'ERROR' and doc['score'] is None and doc['reward_eligible'] is False
        assert (p['code'], p['retryable'], p['metadata']['started']) == ('EVALUATION_TIMEOUT', False, False)
        assert p['where']['stage'] in ('queued', 'installing')
        assert p['where']['stage'] == p['metadata']['stage'] == doc['outcome']['stage']
        assert p['detail'].startswith('The evaluation reached its deadline during the ')
        assert doc['outcome']['fault'] == 'timeout' and doc['outcome']['code'] == 'EVALUATION_TIMEOUT'
        assert doc['outcome']['started'] is False and doc['timing']['ran_s'] is None
        assert doc['timing']['started_at'] is None and doc['timing']['queued_s'] >= 0
        assert stub_state(runtime, sha, 'late') is None                          # never handed to the evaluator
        assert not runtime.job('late')['forwarded']


def test_waiting_request_times_out_while_the_running_one_continues(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=3)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='first'))
        wait_until(lambda: runtime.job('first')['forwarded'])
        request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='second', time_limit_s='1'))
        doc = json.loads(wait_until(lambda: terminal(request, 'second'))[2])
        assert doc['problems'][0]['code'] == 'EVALUATION_TIMEOUT' and doc['problems'][0]['where']['stage'] == 'queued'
        assert terminal(request, 'first') is None                                # still running
        assert wait_until(lambda: terminal(request, 'first'))[2] == RAW          # and completes untouched
        assert stub_state(runtime, sha, 'second') is None


def test_old_evaluator_is_stopped_at_the_deadline_through_the_fence(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=12)           # no cf-eval-ops marker; runs 6-12 s
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        fields = dict(design_zip=b'a', evaluator_zip=archive, request_id='long')
        request('POST', '/evaluate', dict(fields, time_limit_s='600'))
        wait_until(lambda: runtime.job('long')['phase'] == 'running', timeout=60)
        assert set(stub_state(runtime, sha, 'long')) == {'time', 'design_sha256', 'chain_commit'}   # no new fields
        # the validator re-sends with a tighter deadline (header form): the loader stops the run at it
        request('POST', '/evaluate', fields, headers={'X-CF-Deadline': f'{time.time() + 1:.3f}', 'X-CF-Request-Id': 'long'})
        doc = json.loads(wait_until(lambda: terminal(request, 'long'), timeout=20)[2])
        assert doc['problems'][0]['code'] == 'EVALUATION_TIMEOUT' and doc['outcome']['started'] is True
        fenced = runtime.root / 'state' / sha / 'long.cancelled'               # the stub's --fence-request record
        assert json.loads(fenced.read_text())['problems'][0]['code'] == 'EVALUATION_TIMEOUT'
        request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='next'))
        wait_until(lambda: runtime.job('next')['forwarded'], timeout=30)       # the machine is free again


def test_ops_evaluator_gets_the_deadline_progress_and_a_grace(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=12, ops=True)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        dl = time.time() + 600
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='ops', deadline_unix=f'{dl:.3f}'))
        wait_until(lambda: runtime.job('ops')['phase'] == 'running', timeout=60)
        state = stub_state(runtime, sha, 'ops')
        assert float(state['deadline_unix']) == pytest.approx(dl, abs=0.001)
        assert float(state['accepted_unix']) == pytest.approx(runtime.job('ops')['created'], abs=0.001)
        status, headers, body = request('GET', '/v2/status/ops')
        doc = json.loads(body)
        assert status == 202 and headers['X-CF-Stage'] == 'compile' and doc['stage'] == 'compile'
        assert doc['tier'] == 'T1' and doc['started_at'] and doc['queue_position'] is None
        assert doc['progress'] == [dict(stage='admit', started_unix=doc['progress'][0]['started_unix'],
                                        finished_unix=doc['progress'][0]['finished_unix'], attempt=1)]
        assert 'score' not in body.decode() and 'bad stage' not in body.decode()
        # Past its deadline, the evaluator (which enforces it itself) gets 90 s before the loader steps in.
        dl = time.time() + 0.5
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_sha256=sha, request_id='ops', deadline_unix=f'{dl:.3f}'))
        assert runtime.job('ops')['deadline'] == pytest.approx(dl, abs=0.001)
        wait_until(lambda: time.time() > dl + 1.5, timeout=10)
        assert runtime.job('ops')['phase'] == 'running' and not (runtime.root / 'state' / sha / 'ops.cancelled').exists()
        assert wait_until(lambda: terminal(request, 'ops'), timeout=20)[2] == RAW


def test_restart_does_not_resume_stale_requests(tmp_path, monkeypatch):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='busy'))
        wait_until(lambda: runtime.job('busy')['forwarded'])
        for rid, extra in (('expired', dict(time_limit_s='3')), ('ancient', {}), ('fresh', {}), ('soon', dict(time_limit_s='3000'))):
            request('POST', '/evaluate', dict(design_zip=rid.encode(), evaluator_sha256=sha, request_id=rid, **extra))
    db = sqlite3.connect(tmp_path / 'root/loader/http.sqlite')
    with db:
        db.execute("UPDATE jobs SET created=created-30000 WHERE request_id IN ('ancient','busy')")  # no deadline, > 7 h 40 min
        db.execute("UPDATE jobs SET deadline=? WHERE request_id='expired'", (time.time() - 1,))
    db.close()
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        for rid in ('expired', 'ancient', 'busy'):
            doc = json.loads(wait_until(lambda: terminal(request, rid))[2])
            assert doc['problems'][0]['code'] == 'EVALUATION_TIMEOUT', rid
        assert 'stale after restart' in problem(terminal(request, 'ancient')[2])['metadata']['reason']
        assert stub_state(runtime, sha, 'expired') is None and stub_state(runtime, sha, 'ancient') is None
        wait_until(lambda: runtime.job('fresh')['forwarded'])                  # what is still wanted runs
        assert runtime.job('soon')['phase'] != 'done'


# ------------------------------------------------------------------ HTTP cancel, cancel-all, jobs, health
def test_admin_routes_token_jobs_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv('V2_ADMIN_TOKEN', 's3cret')
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60, ops=True)
    auth = {'Authorization': 'Bearer s3cret'}
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='run'))
        wait_until(lambda: runtime.job('run')['phase'] == 'running', timeout=60)
        dl = int(time.time() + 3000)
        request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='wait1', deadline_unix=str(dl)))
        request('POST', '/evaluate', dict(design_zip=b'c', evaluator_sha256=sha, request_id='wait2'))
        for path in ('/jobs', '/v2/jobs'):
            assert request('GET', path)[0] == 401
            assert request('GET', path, headers={'Authorization': 'Bearer wrong'})[0] == 401
        assert request('POST', '/v2/cancel/run')[0] == 401 and request('POST', '/v2/cancel-all')[0] == 401
        status, _, body = request('GET', '/v2/jobs', headers=auth)
        jobs = {j['request_id']: j for j in json.loads(body)['jobs']}
        assert status == 200 and list(jobs) == ['run', 'wait1', 'wait2']
        assert (jobs['run']['phase'], jobs['run']['queue_position'], jobs['run']['stage']) == ('running', None, 'compile')
        assert jobs['run']['started_at'] and jobs['run']['sent_at']
        assert (jobs['wait1']['phase'], jobs['wait1']['queue_position'], jobs['wait1']['deadline_unix']) == ('queued', 1, float(int(dl)))
        assert (jobs['wait2']['queue_position'], jobs['wait2']['deadline']) == (2, None)
        assert json.loads(request('GET', '/v2/status/wait2')[2])['queue_position'] == 2
        # one request: waiting -> finished by the loader; running -> by its evaluator; unknown -> 404
        status, _, body = request('POST', '/v2/cancel/wait1', headers={**auth, 'X-CF-Reason': 'validator stopped\twaiting'})
        assert status == 200 and json.loads(body)['by'] == 'loader'
        p = problem(terminal(request, 'wait1')[2])
        assert p['code'] == 'EVALUATION_CANCELLED' and p['metadata']['by'] == 'validator'
        assert 'validator stoppedwaiting' in p['detail']                     # control characters dropped
        status, _, body = request('POST', '/cancel/run', headers=auth)
        assert status == 200 and json.loads(body)['by'] == 'evaluator'
        fenced = json.loads((runtime.root / 'state' / sha / 'run.cancelled').read_text())   # the fence's record
        assert fenced['problems'][0]['metadata'] == {'by': 'validator', 'stage': 'compile'}
        assert request('POST', '/cancel/no-such', headers=auth)[0] == 404
        assert request('POST', '/cancel/-rf', headers=auth)[0] == 404
        assert request('GET', '/cancel/run', headers=auth)[0] == 405
        status, _, body = request('POST', '/cancel/wait1', headers=auth)
        assert status == 200 and json.loads(body)['reason'].startswith('already finished')
        # the kill switch: everything unfinished
        request('POST', '/evaluate', dict(design_zip=b'd', evaluator_sha256=sha, request_id='wait3'))
        status, _, body = request('POST', '/v2/cancel-all', headers={**auth, 'X-CF-Reason': 'kill switch'})
        out = json.loads(body)
        assert status == 200 and set(out['cancelled']) >= {'wait2', 'wait3'} and out['failed'] == []
        for rid in ('wait2', 'wait3'):
            p = problem(wait_until(lambda: terminal(request, rid))[2])
            assert p['code'] == 'EVALUATION_CANCELLED' and p['metadata']['by'] == 'kill_switch'
        jobs = json.loads(request('GET', '/jobs', headers=auth)[2])['jobs']
        assert {j['phase'] for j in jobs} == {'done'}                         # recently finished, all done
        health = json.loads(request('GET', '/health')[2])                      # health stays open
        assert health['api'] == 'cf-eval-ops/1' and 'cancel' in health['features']
        assert health['disk']['total_gib'] > 0 and health['queue'] == dict(installing=0, queued=0, running=0, oldest_queued_s=None)
    monkeypatch.delenv('V2_ADMIN_TOKEN')
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        assert request('GET', '/jobs')[0] == 200                               # open when no token is set


def test_cf_cancel_cli_keeps_working(tmp_path, monkeypatch, capsys):
    from loader.cancel import main
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        monkeypatch.setenv('CF_V2_ROOT', str(runtime.root))
        monkeypatch.setenv('CF_EVALUATOR_SALT_FILE', str(runtime.installer.salt))
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='a1'))
        wait_until(lambda: runtime.job('a1')['forwarded'])
        request('POST', '/evaluate', dict(design_zip=b'b', evaluator_sha256=sha, request_id='a2'))
        request('POST', '/evaluate', dict(design_zip=b'c', evaluator_sha256=sha, request_id='a3'))
        # the gateway's exact call shape: cf-cancel --reason R -- <id>
        assert main(['--reason', 'gave up (validator)', '--', 'a2']) == 0
        assert json.loads(capsys.readouterr().out)['cancelled'] is True
        assert problem(terminal(request, 'a2')[2])['metadata']['by'] == 'operator'
        assert main(['--reason', 'x', '--', 'missing']) == 3
        capsys.readouterr()
        assert main(['--by', 'watchdog', '--', 'a1']) == 0                    # running: through the fence
        assert problem((runtime.root / 'state' / sha / 'a1.cancelled').read_bytes())['metadata']['by'] == 'watchdog'
        capsys.readouterr()
        assert main(['--all', '--reason', 'kill']) == 0
        assert json.loads(capsys.readouterr().out) == {'cancelled': ['a3'], 'already_finished': [], 'failed': []}
        with pytest.raises(SystemExit):
            main([])


# ------------------------------------------------------------------ housekeeping
def test_uploads_deleted_retention_and_retry_restores_bytes(tmp_path, monkeypatch):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=0.2)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='done1'))
        wait_until(lambda: terminal(request, 'done1'))
        uploads = runtime.root / 'loader/uploads'
        (uploads / 'orphan.zip').write_bytes(b'x')                           # a crash between upload and acceptance
        runtime.sweep_uploads()
        assert list(uploads.iterdir()) == []
        # a system failure retried under the same ID gets its bytes back (a real install failure, r3 review:
        # never a hand-made forwarded=0 on a forwarded row, a state production never makes)
        failing = tmp_path / 'f.zip'; build_zip(failing, delay=0.2, fail_preflight=True)
        assert request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=failing, request_id='retry'))[0] == 202
        assert json.loads(wait_until(lambda: terminal(request, 'retry'), timeout=60)[2])['problems'][0]['retryable'] is True
        runtime.sweep_uploads()
        assert not os.path.exists(runtime.job('retry')['design'])
        (runtime.root / 'permit-preflight').write_text('')
        assert request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=failing, request_id='retry'))[0] == 202
        assert open(runtime.job('retry')['design'], 'rb').read() == b'a'
        wait_until(lambda: terminal(request, 'retry') and runtime.job('retry')['error'] is None, timeout=60)
        # request IDs expire CF_V2_REQUEST_RETENTION_DAYS after finishing; unfinished ones never do
        with runtime.db() as db:
            db.execute("UPDATE jobs SET finished=finished-8*86400 WHERE request_id='done1'")
            db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created) "
                       "VALUES('old-open',?,1,'T2','s','h','/nonexistent','installing_evaluator',?)", (sha, time.time() - 9 * 86400))
        monkeypatch.setattr(runtime, 'expire', lambda *a, **k: False)          # keep the unfinished row as it is
        runtime.housekeeping()
        assert runtime.job('done1') is None and runtime.job('retry') is not None
        assert runtime.job('old-open') is not None


def _install_failure_then_retry(runtime, request, archive, rid, **retry):
    """First POST, no deadline: the evaluator install fails (a done, retryable system ERROR, never forwarded).
    The operator repairs the cause and the validator retries the same ID with the given fields."""
    assert request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id=rid))[0] == 202
    first = json.loads(wait_until(lambda: terminal(request, rid), timeout=60)[2])
    assert first['problems'][0]['fault'] == 'system' and first['problems'][0]['retryable'] is True
    assert runtime.job(rid)['deadline'] is None and not runtime.job(rid)['forwarded']
    (runtime.root / 'permit-preflight').write_text('')
    return request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id=rid, **retry))


def test_retry_after_system_failure_takes_the_deadline_it_is_resent_with(tmp_path):
    # Review r1 MUST-FIX: a retry (same ID, after a done system ERROR) used to keep the first POST's deadline,
    # so a first POST without one left the retried run with none at all.
    archive = tmp_path / 'e.zip'; build_zip(archive, delay=60, fail_preflight=True)   # old zip: the loader enforces
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        dl = time.time() + 8
        assert _install_failure_then_retry(runtime, request, archive, 'rt', deadline_unix=f'{dl:.3f}')[0] == 202
        assert runtime.job('rt')['deadline'] == pytest.approx(dl, abs=0.001)
        doc = json.loads(wait_until(lambda: terminal(request, 'rt'), timeout=40)[2])
        assert doc['problems'][0]['code'] == 'EVALUATION_TIMEOUT'
        assert doc['timing']['deadline_unix'] == pytest.approx(dl, abs=0.001)


def test_retry_keeps_the_earlier_deadline_and_forwards_it_to_an_ops_evaluator(tmp_path):
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=60, ops=True, fail_preflight=True)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        dl = time.time() + 600
        assert _install_failure_then_retry(runtime, request, archive, 'rto', deadline_unix=f'{dl:.3f}')[0] == 202
        wait_until(lambda: runtime.job('rto')['phase'] == 'running', timeout=60)
        assert float(stub_state(runtime, sha, 'rto')['deadline_unix']) == pytest.approx(dl, abs=0.001)
        # tighten-only still holds for re-sends of the live retried job
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_sha256=sha, request_id='rto', deadline_unix=f'{dl + 300:.3f}'))
        assert runtime.job('rto')['deadline'] == pytest.approx(dl, abs=0.001)


def test_retry_deadline_is_the_earlier_of_stored_and_resent(tmp_path):
    archive = tmp_path / 'e.zip'; build_zip(archive, delay=60, fail_preflight=True)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        dl = time.time() + 600
        assert request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='rte',
                                                 deadline_unix=f'{dl:.3f}'))[0] == 202
        assert json.loads(wait_until(lambda: terminal(request, 'rte'), timeout=60)[2])['problems'][0]['fault'] == 'system'
        (runtime.root / 'permit-preflight').write_text('')
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='rte', deadline_unix=f'{dl + 300:.3f}'))
        assert runtime.job('rte')['deadline'] == pytest.approx(dl, abs=0.001)            # looser: not taken
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='rte'))
        assert runtime.job('rte')['deadline'] == pytest.approx(dl, abs=0.001)            # absent: kept


def test_old_evaluator_logs_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv('CF_V2_LOG_MAX_MIB', '1')
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=0.2)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='x'))
        wait_until(lambda: terminal(request, 'x'))
        log = runtime.root / 'state' / sha / 'workers.log'
        log.write_bytes(b'w' * (2 << 20))
        runtime.cap_logs()
        assert log.stat().st_size == 0 and (log.with_name('workers.log.1')).stat().st_size == 2 << 20
    archive2 = tmp_path / 'o.zip'; sha2 = build_zip(archive2, delay=0.2, ops=True)
    with running(tmp_path / 'b', allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive2, request_id='y'))
        wait_until(lambda: terminal(request, 'y'))
        log = runtime.root / 'state' / sha2 / 'workers.log'
        log.write_bytes(b'w' * (2 << 20))
        runtime.cap_logs()                                                   # a cf-eval-ops/1 evaluator caps its own
        assert log.stat().st_size == 2 << 20


def test_status_polls_follow_the_evaluator_pace(tmp_path, monkeypatch):
    from loader.supervisor import Service
    polls = []
    original = Service.request

    def counted(self, method, path, *a, **k):
        if path.startswith('/status/'):
            polls.append(time.monotonic())
        return original(self, method, path, *a, **k)
    monkeypatch.setattr(Service, 'request', counted)
    archive = tmp_path / 'e.zip'; build_zip(archive, delay=60)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        request('POST', '/evaluate', dict(design_zip=b'a', evaluator_zip=archive, request_id='p'))
        wait_until(lambda: runtime.job('p')['forwarded'])
        polls.clear()
        time.sleep(3.2)
    # was one poll per 0.25 s (12+ here); now one per Retry-After (1 s when the evaluator names none, at most 5 s)
    assert 2 <= len(polls) <= 5


def test_loader_errors_carry_outcome_and_timing_and_no_token_reaches_the_evaluator(tmp_path, monkeypatch):
    now = time.time()
    job = dict(request_id='r', submitter='s', mode='T2', created=now - 30, started=now - 10, stage='compile',
               tier='T1', deadline=now - 1)
    doc = json.loads(timeout_error(job, 'compile'))
    assert doc['outcome'] == dict(fault='timeout', code='EVALUATION_TIMEOUT', stage='compile', reason=doc['outcome']['reason'], started=True)
    assert doc['tier_reached'] == 'T1' and doc['problems'][0]['where'] == {'tier': 'T1', 'stage': 'compile'}
    assert 19 < doc['timing']['queued_s'] < 21 and 9 < doc['timing']['ran_s'] < 12 and doc['timing']['deadline_unix'] == now - 1
    assert len(doc['outcome']['reason']) <= 160 and doc['timing']['finished_at'].endswith('Z')
    legacy = json.loads(system_error(dict(request_id='r', submitter='s', mode='T2'), 'EVALUATOR_INSTALL_FAILED'))
    assert legacy['outcome']['fault'] == 'system' and legacy['problems'][0]['where'] == {'tier': 'T0', 'stage': 'evaluator_install'}
    assert legacy['problems'][0]['retryable'] is True and legacy['status'] == 'ERROR' and legacy['score'] is None
    from loader.install import environment
    monkeypatch.setenv('V2_ADMIN_TOKEN', 'tok')
    assert 'V2_ADMIN_TOKEN' not in environment(tmp_path, 'a' * 64, tmp_path / 'salt')


# ------------------------------------------------------------------ r3 review fixes
def _queued(tmp_path, monkeypatch, deadline, forwarded=0, design='unused', clock=None):
    """A Runtime with no dispatcher and one queued row; the loader's clock is `clock` (a one-element list)."""
    from types import SimpleNamespace
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    runtime = Runtime(tmp_path / 'root', tmp_path / 'salt', allow_unsigned=True)
    with runtime.db() as db:
        db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,forwarded,sent,deadline) "
                   "VALUES('q',?,1,'T2','miner','hash',?,'queued',900,?,?,?)", ('a' * 64, design, forwarded, forwarded, deadline))
    sent = []
    monkeypatch.setattr(runtime.service, 'ensure', lambda sha: None)
    monkeypatch.setattr(runtime.service, 'request', lambda *a, **k: (404, {}, b'{}'))
    monkeypatch.setattr(runtime.service, 'submit', lambda job: sent.append(dict(job)) or (202, {}, b'{"state":"queued"}'))
    return runtime, sent


def _timed_out_unsent(runtime):
    job = runtime.job('q')
    doc = json.loads(job['response'])
    assert (job['phase'], job['error'], job['forwarded'], job['sent']) == ('done', 'EVALUATION_TIMEOUT', 0, 0)
    assert doc['outcome']['started'] is False and doc['problems'][0]['where']['stage'] == 'queued'


def test_slow_service_start_past_the_deadline_never_forwards(tmp_path, monkeypatch):
    # Codex r3 MF1: expiry ran before tick(); a slow service start crossed the deadline and the request was sent.
    clock = [1000.0]
    runtime, sent = _queued(tmp_path, monkeypatch, 1001.0, clock=clock)
    monkeypatch.setattr(runtime.service, 'ensure', lambda sha: clock.__setitem__(0, 1002.0))
    try:
        runtime.tick(runtime.job('q'))
        assert sent == []
        _timed_out_unsent(runtime)
    finally:
        runtime.close()


def test_deadline_tightened_after_the_snapshot_is_the_one_enforced_and_forwarded(tmp_path, monkeypatch):
    clock = [1000.0]
    runtime, sent = _queued(tmp_path, monkeypatch, 999.0, clock=clock)        # tightened to the past
    try:
        snapshot = dict(runtime.job('q'), deadline=1001.0)                       # the pass's stale view
        runtime.tick(snapshot)
        assert sent == []
        _timed_out_unsent(runtime)
    finally:
        runtime.close()
    clock = [1000.0]
    runtime, sent = _queued(tmp_path / 'b', monkeypatch, 1500.0, clock=clock)   # tightened, still ahead
    try:
        runtime.tick(dict(runtime.job('q'), deadline=2000.0))
        assert [j['deadline'] for j in sent] == [1500.0]                         # the current one is forwarded
        assert (runtime.job('q')['forwarded'], runtime.job('q')['sent']) == (1, 1)
    finally:
        runtime.close()


def test_body_preparation_crossing_the_deadline_never_sends(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from loader import supervisor
    from loader.supervisor import Service
    clock = [1000.0]
    design = tmp_path / 'design.zip'; design.write_bytes(b'd' * 4096)
    runtime, _ = _queued(tmp_path, monkeypatch, 1001.0, design=str(design), clock=clock)
    posts = []
    monkeypatch.setattr(runtime.service, 'submit', Service.submit.__get__(runtime.service))
    monkeypatch.setattr(runtime.service, 'request', lambda method, path, *a: posts.append(path) or (404, {}, b'{}'))
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(time=lambda: clock[0]))
    # the body is written slowly (ops_api is consulted while it is written): the clock crosses the deadline
    monkeypatch.setattr(supervisor, 'ops_api', lambda *a: clock.__setitem__(0, 1002.0) or False)
    try:
        runtime.tick(runtime.job('q'))
        assert posts == ['/status/q']                                            # never POST /evaluate
        _timed_out_unsent(runtime)
    finally:
        runtime.close()


def test_lost_forwarded_request_past_its_deadline_is_not_resubmitted(tmp_path, monkeypatch):
    clock = [1000.0]
    runtime, sent = _queued(tmp_path, monkeypatch, 999.0, forwarded=1, clock=clock)
    fenced = []
    monkeypatch.setattr(runtime_module, 'fence', lambda root, salt, job, body: fenced.append(json.loads(body)) or
                        {'fenced': True, 'drained': True})
    try:
        runtime.tick(runtime.job('q'))                                           # the evaluator answers 404
        assert sent == [] and fenced[0]['problems'][0]['code'] == 'EVALUATION_TIMEOUT'
        assert (runtime.job('q')['phase'], runtime.job('q')['error']) == ('done', 'EVALUATION_TIMEOUT')
    finally:
        runtime.close()


def test_request_id_reused_after_retention_never_gets_the_earlier_result(tmp_path):
    # Opus r3 MF1 (the reviewer's probe): the loader row expired, the evaluator kept its own record.
    import hashlib
    archive = tmp_path / 'e.zip'; sha = build_zip(archive, delay=0.2)
    with running(tmp_path, allow_unsigned=True) as (runtime, request):
        assert request('POST', '/evaluate', dict(design_zip=b'AAAA', evaluator_zip=archive, request_id='reuse'))[0] == 202
        first = wait_until(lambda: terminal(request, 'reuse'))[2]
        assert first == RAW and runtime.job('reuse')['sent'] == 1
        with runtime.db() as db:
            db.execute("UPDATE jobs SET finished=finished-8*86400 WHERE request_id='reuse'")
        runtime.housekeeping()
        assert runtime.job('reuse') is None
        assert request('POST', '/evaluate', dict(design_zip=b'BBBBBBBB', evaluator_sha256=sha, request_id='reuse'))[0] == 202
        doc = json.loads(wait_until(lambda: terminal(request, 'reuse'))[2])
        p = doc['problems'][0]
        assert (doc['status'], doc['score'], doc['receipt'], p['code'], p['retryable']) == (
            'ERROR', None, None, 'REQUEST_ID_CONFLICT', False)
        state = json.loads((runtime.root / 'state' / sha / 'reuse.json').read_text())
        assert state['design_sha256'] == hashlib.sha256(b'AAAA').hexdigest()     # the new bytes never ran under it
        assert runtime.job('reuse')['sent'] == 0
        # re-sending it is answered with the same conflict, not retried into the earlier result
        again = request('POST', '/evaluate', dict(design_zip=b'BBBBBBBB', evaluator_sha256=sha, request_id='reuse'))
        assert again[0] == 200 and problem(again[2])['code'] == 'REQUEST_ID_CONFLICT'
        # a new ID runs the new bytes
        request('POST', '/evaluate', dict(design_zip=b'BBBBBBBB', evaluator_sha256=sha, request_id='reuse-2'))
        assert wait_until(lambda: terminal(request, 'reuse-2'))[2] == RAW


def test_rows_from_an_older_loader_count_as_sent(tmp_path):
    root = tmp_path / 'root'; (root / 'loader').mkdir(parents=True)
    db = sqlite3.connect(root / 'loader/http.sqlite')
    with db:
        db.executescript(runtime_module.SCHEMA)
        db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,forwarded) "
                   "VALUES('old',?,1,'T2','m','h','unused','queued',0,0)", ('a' * 64,))
    db.close()
    runtime = Runtime(root, tmp_path / 'salt', allow_unsigned=True)
    try:
        assert runtime.job('old')['sent'] == 1                                   # adopted exactly as before
    finally:
        runtime.close()


def test_cancel_and_system_errors_report_one_stage_and_tier():
    # Codex r3 MF2: the problem location said evaluator_install while outcome/metadata said queued or compile.
    from loader.cancel import cancelled_error
    now = time.time()
    queued = dict(request_id='r', submitter='s', mode='T2', created=now - 5)
    run = dict(queued, started=now - 2, stage='compile', tier='T1')
    for job, stage, tier in ((queued, 'queued', 'T0'), (run, 'compile', 'T1')):
        doc = json.loads(cancelled_error(job, 'x', 'operator'))
        p = doc['problems'][0]
        assert doc['outcome']['stage'] == p['where']['stage'] == p['metadata']['stage'] == stage
        assert doc['tier_reached'] == p['where']['tier'] == tier
        doc = json.loads(system_error(job, 'EVALUATOR_SERVICE_EXITED'))
        p = doc['problems'][0]
        assert doc['outcome']['stage'] == p['where']['stage'] == (stage if job is run else 'evaluator_install')
        assert doc['tier_reached'] == p['where']['tier'] == tier
