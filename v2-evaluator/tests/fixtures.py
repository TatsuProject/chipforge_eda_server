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
ap.add_argument('--cancel'); ap.add_argument('--reason')
a = ap.parse_args()
state = Path(a.state)
state.mkdir(parents=True, exist_ok=True)
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
            return self.send(200,b'{{"status":"ERROR","problems":[{{"code":"EVALUATION_CANCELLED"}}]}}')
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
        assert set(fields) == {{'design_zip','request_id','mode','submitter'}}
        rid = fields['request_id'].decode()
        p = state / (rid+'.json')
        if not p.exists(): p.write_text(json.dumps(dict(time=time.time(), design_sha256=hashlib.sha256(fields['design_zip']).hexdigest())))
        return self.send(202,b'{{"state":"queued"}}')
ThreadingHTTPServer((a.host,int(a.port)),H).serve_forever()
'''


def build_zip(path, *, assets=None, delay=0.6, preflight_delay=0, fail_preflight=False, post_install=False, extra=None, c7=True):
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
    files = {'v2/deploy/service.py': STUB.format(raw=RAW, delay=delay),
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
