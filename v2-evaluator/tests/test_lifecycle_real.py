"""Tiny real loader/service fault gate; no EDA synthesis or RAM preflight bypass.

Opt in with CF_REAL_EVALUATOR_SOURCE pointing to the evaluator checkout.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from loader.runtime import Runtime
from loader.server import Server
from loader.install import environment
from loader.cancel import cancel
from test_runtime import wait_until


@pytest.mark.skipif(not os.environ.get('CF_REAL_EVALUATOR_SOURCE'), reason='explicit real evaluator source required')
@pytest.mark.parametrize('terminal_transport', [False, True])
def test_real_service_kill_fences_all_generations_before_terminal(tmp_path, monkeypatch, terminal_transport):
    source = Path(os.environ['CF_REAL_EVALUATOR_SOURCE']).resolve()
    monkeypatch.setenv('CF_MEMORY', '1GiB')  # actual service refuses worker generation; no full EDA work
    monkeypatch.setenv('CF_CPUS', '1')
    monkeypatch.setenv('CF_V2_ROOT', str(tmp_path / 'root'))
    sha = 'b' * 64
    runtime = Runtime(tmp_path / 'root', tmp_path / 'salt', allow_unsigned=True, modes=('DEV',))
    tree = runtime.root / 'evaluators' / sha
    tree.symlink_to(source, target_is_directory=True)
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'DEV': {'workloads': ['fixture'], 'populations': {'ss': 1, 'offline': 0}}}))
    monkeypatch.setattr(runtime.installer, 'ready', lambda _: {'manifest': {'service': 'v2/deploy/service.py', 'policy': str(policy)}})
    env = environment(runtime.root, sha, runtime.installer.salt)
    state = Path(env['CF_ORCH_SSD'])
    chain_base = hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:16]
    owned = []
    boxes = []
    sibling = None
    sibling_box = None
    server = Server(('127.0.0.1', 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def get(path):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()
    try:
        runtime.service.ensure(sha)  # real service, real SQLite queue and transport
        with runtime.db() as db:
            db.execute("INSERT INTO installs VALUES(?,1,'ready',NULL,1)", (sha,))
            db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,forwarded) "
                       "VALUES('real-kill',?,1,'DEV','miner','hash','unused','running',0,1)", (sha,))
        # Seed two real lease generations, with tiny host processes and Docker boxes owned by each.
        seed = '''import json,sys,socket,os
from pathlib import Path
sys.path.insert(0,sys.argv[1]); import service
q=service.connect_orchestrator(sys.argv[2],sys.argv[3])
with service.database(sys.argv[2]) as db:
 db.execute("INSERT INTO requests(request_id,upload_sha256,submitter,mode,policy,upload,created_at) VALUES('real-kill','hash','miner','DEV','{}','unused',0)")
sub,_=q.submit('http:real-kill','unused','c'*64,meta={'mode':'DEV'})
a=q.claim(q.register_worker('w-gen1'))
with q._txn() as db: db.execute('UPDATE submissions SET lease_expires=0 WHERE id=?',(sub,))
b=q.claim(q.register_worker('w-gen2'))
with service.database(sys.argv[2]) as db: db.execute("UPDATE requests SET job_id=? WHERE request_id='real-kill'",(sub,))
print(sub)
'''
        job = subprocess.check_output([sys.executable, '-B', '-c', seed, str(source / 'v2/deploy'), str(state), env['CF_ORCH_POSIX']],
                                      env=env, text=True).strip().splitlines()[-1]
        chain = hashlib.sha256(str((state / 'state/queue.sqlite').resolve()).encode()).hexdigest()[:16] + '/' + job
        for n in (1, 2):
            labels = f'cf.orch.service={chain_base},cf.orch.job={job},cf.orch.v8.worker=w-gen{n}'
            proc_env = dict(env, CF_DOCKER_LABELS=labels, CF_ORCH_RUN_CHAIN=chain + f'/gen{n}', CF_ORCH_V8_WORKER=f'w-gen{n}')
            proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(180)', '--child', 'worker'], env=proc_env, start_new_session=True)
            owned.append(proc)
            stage = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(180)'],
                                     env=proc_env, start_new_session=True)
            owned.append(stage)
            with sqlite3.connect(state / 'state/queue.sqlite') as db:
                db.execute('UPDATE workers SET pid=?,started_at=? WHERE worker_id=?', (proc.pid, time.time(), f'w-gen{n}'))
            cid = subprocess.check_output(['docker', 'create', '--network', 'none', '--memory', '32m', '--memory-swap', '32m',
                    '--label', f'cf.orch.service={chain_base}', '--label', f'cf.orch.job={job}',
                    '--label', f'cf.orch.v8.worker=w-gen{n}', 'alpine:latest', 'nice', '-n', '10', 'sleep', '180'], text=True).strip()
            boxes.append(cid)
            subprocess.run(['docker', 'start', cid], check=True, capture_output=True)
        # Exact ownership filters must leave another job in the same service untouched.
        sibling_env = dict(env, CF_DOCKER_LABELS=f'cf.orch.service={chain_base},cf.orch.job={job}-other',
                           CF_ORCH_RUN_CHAIN=chain + '-other/stage')
        sibling = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(180)'],
                                   env=sibling_env, start_new_session=True)
        sibling_box = subprocess.check_output(['docker', 'create', '--network', 'none', '--memory', '32m',
                    '--memory-swap', '32m', '--label', f'cf.orch.service={chain_base}',
                    '--label', f'cf.orch.job={job}-other', 'alpine:latest', 'nice', '-n', '10', 'sleep', '180'], text=True).strip()
        subprocess.run(['docker', 'start', sibling_box], check=True, capture_output=True)
        runtime.tools_ready = True
        if terminal_transport:
            from loader.runtime import system_error
            body = system_error(runtime.job('real-kill'), 'EVALUATOR_SERVICE_EXITED')
            with runtime.db() as db:
                db.execute("UPDATE jobs SET phase='done',response=?,response_type='application/json',error='EVALUATOR_SERVICE_EXITED'", (body,))
            before_cancel = runtime.job('real-kill')
            assert cancel(runtime.root, runtime.installer.salt, 'real-kill', 'drain legacy terminal orphan')[1]['drained']
            assert runtime.job('real-kill') == before_cancel
        runtime.start()
        old = runtime.service.proc
        os.kill(old.pid, signal.SIGKILL)
        started = time.monotonic()
        terminal = wait_until(lambda: (r if (r := get('/status/real-kill'))[0] == 200 else None), timeout=45)
        elapsed = time.monotonic() - started
        assert terminal[1]['problems'][0]['code'] == 'EVALUATOR_SERVICE_EXITED'
        assert terminal[1]['score'] is None
        assert all(p.wait(timeout=5) == -signal.SIGKILL for p in owned)
        def backend():
            with sqlite3.connect(state / 'state/queue.sqlite') as db:
                return db.execute('SELECT state,attempts,lease_token FROM submissions WHERE id=?', (job,)).fetchone()
        assert backend()[:2] == ('done', 2)
        for cid in boxes:
            assert subprocess.run(['docker', 'inspect', cid], capture_output=True).returncode != 0
        assert sibling.poll() is None
        assert subprocess.run(['docker', 'inspect', sibling_box], capture_output=True).returncode == 0
        before = runtime.job('real-kill')
        assert cancel(runtime.root, runtime.installer.salt, 'real-kill', 'terminal orphan audit')[1]['drained']
        assert runtime.job('real-kill') == before
        wait_until(lambda: runtime.service.proc and runtime.service.proc.pid != old.pid, timeout=20)
        # Force the old deadline into the past, then run real queue recovery repeatedly after restart.
        recovery = '''import sys
sys.path.insert(0,sys.argv[1]); import service
q=service.connect_orchestrator(sys.argv[2],sys.argv[3])
with q._txn() as db: db.execute('UPDATE submissions SET lease_expires=0')
for _ in range(3):
 assert q.recover_expired()==[]
 assert q.claim('must-not-replay') is None
print('NO_REPLAY_AFTER_LEASE_EXPIRY')
'''
        output = subprocess.check_output([sys.executable, '-B', '-c', recovery, str(source / 'v2/deploy'), str(state), env['CF_ORCH_POSIX']], env=env, text=True)
        assert 'NO_REPLAY' in output
        assert backend()[:2] == ('done', 2)
        assert get('/status/real-kill')[1] == terminal[1]
        assert get('/health')[0] in (200, 503)  # loader remains responsive; deliberate RAM refusal is retained
        print(json.dumps({'terminal_s': elapsed, 'backend': backend(), 'owned_processes': len(owned), 'owned_boxes': len(boxes), 'restarted_pid': runtime.service.proc.pid}))
    finally:
        server.shutdown(); server.server_close(); thread.join(); runtime.close()
        for proc in owned:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        for cid in boxes:
            subprocess.run(['docker', 'rm', '-f', cid], capture_output=True)
        if sibling is not None:
            if sibling.poll() is None:
                sibling.kill()
            sibling.wait()
        if sibling_box is not None:
            subprocess.run(['docker', 'rm', '-f', sibling_box], capture_output=True)


@pytest.mark.skipif(not os.environ.get('CF_REAL_EVALUATOR_SOURCE'), reason='explicit real evaluator source required')
@pytest.mark.parametrize('verdict', ['PASSED', 'REJECTED'])
def test_service_sigkill_after_queue_completion_preserves_verdict(tmp_path, monkeypatch, verdict):
    source = Path(os.environ['CF_REAL_EVALUATOR_SOURCE']).resolve()
    monkeypatch.setenv('CF_MEMORY', '1GiB')
    monkeypatch.setenv('CF_CPUS', '1')
    monkeypatch.setenv('CF_V2_ROOT', str(tmp_path / 'root'))
    sha = 'b' * 64
    runtime = Runtime(tmp_path / 'root', tmp_path / 'salt', allow_unsigned=True, modes=('DEV',))
    (runtime.root / 'evaluators' / sha).symlink_to(source, target_is_directory=True)
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'DEV': {'workloads': ['fixture'], 'populations': {'ss': 1, 'offline': 0}}}))
    monkeypatch.setattr(runtime.installer, 'ready', lambda _: {'manifest': {'service': 'v2/deploy/service.py', 'policy': str(policy)}})
    env = environment(runtime.root, sha, runtime.installer.salt)
    real = {'request_id': 'completed', 'status': verdict, 'score': {'score': 1} if verdict == 'PASSED' else None}
    try:
        runtime.service.ensure(sha)
        with runtime.db() as db:
            db.execute("INSERT INTO jobs(request_id,sha,attempt,mode,submitter,design_hash,design,phase,created,forwarded) "
                       "VALUES('completed',?,1,'DEV','miner','hash','unused','running',0,1)", (sha,))
        seed = 'import json,sys\nsys.path.insert(0,sys.argv[1]); import service\nstate,posix=sys.argv[2:4]\nq=service.connect_orchestrator(state,posix)\nwith service.database(state) as db:\n db.execute("INSERT INTO requests(request_id,upload_sha256,submitter,mode,policy,upload,created_at) VALUES(\'completed\',\'hash\',\'miner\',\'DEV\',\'{}\',\'unused\',0)")\nsub,_=q.submit(\'http:completed\',\'unused\',\'c\'*64,meta={\'mode\':\'DEV\'})\nlease=q.claim(q.register_worker(\'completed-worker\'))\nreal=json.loads(sys.argv[4])\nq.complete(lease,real[\'status\'],\'a\'*64,response=real)\nwith service.database(state) as db: db.execute("UPDATE requests SET job_id=? WHERE request_id=\'completed\'",(sub,))\n'
        subprocess.run([sys.executable, '-B', '-c', seed, str(source / 'v2/deploy'), env['CF_ORCH_SSD'],
                        env['CF_ORCH_POSIX'], json.dumps(real)], env=env, check=True)
        # No loader dispatch/poll has occurred since the durable queue completion.
        os.kill(runtime.service.proc.pid, signal.SIGKILL)
        runtime.service.proc.wait(timeout=10)
        runtime.tick(runtime.job('completed'))
        row = runtime.job('completed')
        assert row['phase'] == 'done'
        assert json.loads(row['response']) == real
        assert row['error'] is None
    finally:
        runtime.close()
