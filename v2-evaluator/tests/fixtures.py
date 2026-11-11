"""Synthetic transport fixture only: no evaluator implementation or challenge data."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

RAW = (b'{ "schema":"cf-response/1", "request_id":"synthetic-job",'
       b' "instance":"urn:chipforge:eval:synthetic-job", "created_at":"2026-01-01T00:00:00Z",'
       b' "submission":{}, "mode":"T2", "status":"INCOMPLETE", "pending":["synthetic fixture"],'
       b' "tier_reached":"T0", "summary":"Synthetic transport fixture", "score":null,'
       b' "reward_eligible":false, "stages":[], "problems":[], "logs":[], "receipt":null, "timing":{},'
       b' "public_report":{"schema":"cf-public-report/1","stub":true,"future":[1,2],"note":"caf\xc3\xa9"} }\n')

STUB = '''import argparse, hashlib, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ap = argparse.ArgumentParser()
for x in ('host','port','state','posix','policy','workers','cores','lanes'): ap.add_argument('--'+x)
ap.add_argument('--allow-runc', action='store_true')
ap.add_argument('--cancel'); ap.add_argument('--reason'); ap.add_argument('--fence-request')
a = ap.parse_args()
state = Path(a.state)
state.mkdir(parents=True, exist_ok=True)
if a.fence_request:
    import sys
    body = json.load(sys.stdin)
    (state / (a.fence_request + '.cancelled')).write_text(json.dumps(body))
    # drained only when the stub's runs really stopped; a test sets fence-not-drained to model a drain that fails
    print(json.dumps(dict(fenced=True, drained=not (state / 'fence-not-drained').exists()))); raise SystemExit(0)
if a.cancel:                                   # operator cancellation (service.py --cancel contract)
    p = state / (a.cancel + '.json')
    if not p.exists():
        print(json.dumps(dict(request_id=a.cancel, cancelled=False))); raise SystemExit(3)
    p.with_suffix('.cancelled').write_text(a.reason or '')
    print(json.dumps(dict(request_id=a.cancel, cancelled=True))); raise SystemExit(0)
RAW = {raw!r}
DELAY = {delay!r}
class H(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == '/health': return self.send(200,b'{{"status":"ok"}}')
        p = state / (self.path.rsplit('/',1)[-1] + '.json')
        if not p.exists(): return self.send(404,b'{{}}')
        if p.with_suffix('.cancelled').exists():
            return self.send(200,p.with_suffix('.cancelled').read_bytes())
        if p.with_suffix('.done-without-response').exists():   # an older evaluator's terminal row, no result
            return self.send(202,b'{{"state":"done"}}')
        t = json.loads(p.read_text())['time']
        if time.time() - t < DELAY:
            phase = 'queued' if time.time() - t < DELAY/2 else 'running'
            return self.send(202,json.dumps(dict(state=phase)).encode())
        return self.send(200,RAW)
    def do_POST(self):
        from email.parser import BytesParser
        import email.policy
        n = int(self.headers['Content-Length'])
        body = self.rfile.read(n)
        msg = BytesParser(policy=email.policy.default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\\r\\n\\r\\n').encode()+body)
        fields = {{part.get_param('name', header='content-disposition'):part.get_payload(decode=True) for part in msg.iter_parts()}}
        assert set(fields) - {{'chain_commit'}} == {{'design_zip','request_id','mode','submitter'}}
        if fields['design_zip'] == b'refused-by-evaluator':
            return self.send(400,b'{{"error":{{"code":"CHAIN_COMMIT_REQUIRED","message":"x"}}}}')
        rid = fields['request_id'].decode()
        p = state / (rid+'.json')
        chain = fields['chain_commit'].decode() if 'chain_commit' in fields else None
        if not p.exists(): p.write_text(json.dumps(dict(time=time.time(), design_sha256=hashlib.sha256(fields['design_zip']).hexdigest(), chain_commit=chain)))
        return self.send(202,b'{{"state":"queued"}}')
ThreadingHTTPServer((a.host,int(a.port)),H).serve_forever()
'''


def ops_stub(text):
    """The synthetic service as a cf-eval-ops/1 (v2.0.6) evaluator: carries the marker, takes deadline_unix /
    accepted_unix (kept in its state file), answers 202 with stage/tier/progress (names and times only), and
    takes --by. Enforces its deadline like service.py: a re-POST of a held request may only tighten it, and a run
    not finished by its deadline answers its own terminal EVALUATION_TIMEOUT (summary 'stub: evaluator timed out').
    State-file switches for tests: `no-self-timeout` (an evaluator that fails to enforce it), `refuse-repost` (a
    re-POST of a held request answers 503, so a tightened deadline never reaches it). Success bodies carry
    timing.finished_at (the run's own finish time)."""
    def rep(old, new):
        assert text.count(old) == 1, old
        return text.replace(old, new)
    text = 'CF_EVAL_OPS_API = "cf-eval-ops/1"\n' + text
    text = rep("ap.add_argument('--fence-request')", "ap.add_argument('--fence-request'); ap.add_argument('--by')")
    text = rep("p.with_suffix('.cancelled').write_text(a.reason or '')",
               "p.with_suffix('.cancelled').write_text(json.dumps(dict(reason=a.reason, by=a.by)))")
    text = rep("        if not p.exists(): p.write_text(",
               "        if p.exists() and (state / 'refuse-repost').exists():\n"
               "            return self.send(503,b'{\"error\":{\"code\":\"BUSY\"}}')\n"
               "        if p.exists() and fields.get('deadline_unix'):\n"
               "            held = json.loads(p.read_text()); new = fields['deadline_unix'].decode()\n"
               "            if held.get('deadline_unix') is None or float(new) < float(held['deadline_unix']):\n"
               "                held['deadline_unix'] = new; p.write_text(json.dumps(held))\n"
               "        if not p.exists(): p.write_text(")
    text = rep("        t = json.loads(p.read_text())['time']\n",
               "        t = json.loads(p.read_text())['time']\n"
               "        dl = json.loads(p.read_text()).get('deadline_unix')\n"
               "        if dl and time.time() >= float(dl) and t + DELAY > float(dl) and not (state / 'no-self-timeout').exists():\n"
               "            out = p.with_suffix('.timeout')\n"
               "            if not out.exists():\n"
               "                out.write_text(json.dumps(dict(schema='cf-response/1', request_id=p.stem, status='ERROR', score=None, "
               "summary='stub: evaluator timed out', receipt=None, problems=[dict(code='EVALUATION_TIMEOUT', fault='system', "
               "metadata=dict(stage='compile', started=True, deadline_unix=float(dl)))], outcome=dict(fault='timeout', "
               "code='EVALUATION_TIMEOUT', stage='compile', reason='deadline', started=True), "
               "timing=dict(finished_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))))\n"
               "            return self.send(200,out.read_bytes())\n")
    text = rep("        return self.send(200,RAW)\n",
               "        return self.send(200,RAW.replace(b'\"timing\":{}', ('\"timing\":{\"finished_at\":\"%s\"}' % "
               "time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t + DELAY))).encode()))\n")
    text = rep("assert set(fields) - {'chain_commit'} ==", "assert set(fields) - {'chain_commit', 'deadline_unix', 'accepted_unix'} ==")
    text = rep("chain_commit=chain)))",
               "chain_commit=chain, deadline_unix=fields.get('deadline_unix', b'').decode() or None, "
               "accepted_unix=fields.get('accepted_unix', b'').decode() or None)))")
    text = rep("return self.send(202,json.dumps(dict(state=phase)).encode())",
               "return self.send(202,json.dumps(dict(state=phase, stage='T1', tier='T1', current_stage='compile' if phase == 'running' else 'queued', "
               "queue_position=None if phase == 'running' else 1, started_unix=t + DELAY/2 if phase == 'running' else None, "
               "progress=[dict(stage='admit', attempt=1, started_unix=t, finished_unix=t + 0.01, score=99.5), "
               "dict(stage='bad stage!', attempt=1, started_unix=t, finished_unix=None)] if phase == 'running' else [])).encode())")
    return text


def build_zip(path, *, assets=None, delay=0.6, preflight_delay=0, fail_preflight=False, post_install=False, extra=None, c7=True, ops=False):
    manifest = dict(schema='cf-evaluator/1')
    preflight = f'''import os, pathlib, time
root = pathlib.Path(os.environ['CF_V2_ROOT'])
time.sleep({preflight_delay!r})
p = root / 'preflight-count'
p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
(root / 'preflight-argv').write_text(' '.join(__import__('sys').argv[1:]))
assert pathlib.Path(os.environ['CF_EVALUATOR_SALT_FILE']).stat().st_size == 64
assert {not fail_preflight!r} or (root/'permit-preflight').exists()
'''
    assets = assets or []
    asset_doc = {'schema': 'cf-assets/1', 'assets': assets}
    policy_path = 'policy/policy.json' if c7 else 'evaluator/policy.json'
    if c7:
        asset_doc = {'schema': 'cf-evaluator-assets/1', 'assets': [
            dict(id=f'synthetic-{i}', url=a['url'], sha256=a['sha256'], bytes=a.get('size'),
                 dest=a['target'], unpack=a.get('unpack', 'none'), licence='synthetic fixture')
            for i, a in enumerate(assets)]}
    service = STUB.format(raw=RAW, delay=delay)
    files = {'v2/deploy/service.py': ops_stub(service) if ops else service,
             'v2/deploy/preflight.py': preflight,
             policy_path: json.dumps({'T2': {}, 'DEV': {}, 'T1': {}}),
             'assets.json': json.dumps(asset_doc)}
    if post_install:
        data = b'synthetic generated data'
        files['regenerate.py'] = "import os,pathlib; (pathlib.Path(os.environ['CF_V2_ROOT'])/'data').mkdir(exist_ok=True); (pathlib.Path(os.environ['CF_V2_ROOT'])/'data/regenerated').write_bytes(" + repr(data) + ')'
        manifest['post_install'] = [{'argv': ['{python}', '{evaluator}/regenerate.py'],
                                    'outputs': [{'path': 'data/regenerated', 'sha256': hashlib.sha256(data).hexdigest()}]}]
    if extra:
        files.update(extra)
    if c7:
        manifest = dict(schema='cf-evaluator-zip/1', version='0.0.0-test', created='2026-01-01T00:00:00Z',
                        git_commit='0'*40, source_dirty=False, python={}, policy=policy_path, assets='assets.json',
                        entrypoint={'service': 'v2/deploy/service.py', 'preflight': 'v2/deploy/preflight.py'},
                        files={name: hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()
                               for name, value in files.items()},
                        post_install=[dict(id='synthetic-regeneration', argv=['python3', 'regenerate.py'], produces=s['outputs'])
                                      for s in manifest.get('post_install', [])])
    files['MANIFEST.json'] = json.dumps(manifest)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def keypair(directory):
    key = Path(directory) / 'signing-key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    return key, key.with_suffix('.pub').read_text().strip()


def sign(archive, key, namespace='chipforge-evaluator'):
    subprocess.run(['ssh-keygen', '-Y', 'sign', '-q', '-f', str(key), '-n', namespace, str(archive)], check=True)
    return Path(str(archive) + '.sig').read_bytes()


if __name__ == '__main__':
    import sys
    print(build_zip(sys.argv[1]))
