#!/usr/bin/env python3
"""Isolated signed-fixture compose smoke. Uses prebuilt images; never builds or starts v1.

python v2-evaluator/tests/container_smoke.py --work <temporary-internal-disk-directory>
"""
import argparse
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile

from fixtures import RAW, build_zip, keypair, sign


def form(fields):
    boundary = 'smoke-' + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        if isinstance(value, str): value = value.encode()
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value, b'\r\n'])
    chunks.append(f'--{boundary}--\r\n'.encode())
    return b''.join(chunks), 'multipart/form-data; boundary=' + boundary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--image', default='chipforge_eda_server-v2-evaluator:2.0')
    ap.add_argument('--gateway-image', default='chipforge_eda_server-eda-gateway:latest')
    args = ap.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='compose-smoke-', dir=args.work)).absolute()
    repo = Path(__file__).resolve().parents[2]
    project = 'cf-c6-smoke-' + uuid.uuid4().hex[:10]
    env = dict(os.environ, CF_V2_ROOT=str(work / 'data'), CF_V2_SECRETS=str(work / 'secrets'),
               CF_V2_ALLOW_UNSIGNED='0', CF_V2_ALLOWED_MODES='T2', CF_V2_CORES='2', CF_V2_ALLOW_RUNC='0',
               V2_SERVICE_URL='http://v2-evaluator:18080')
    command = []
    try:
        for name in ('data', 'secrets', 'shared', 'results', 'incoming'):
            (work / name).mkdir()
        archive = work / 'fake.zip'
        sha = build_zip(archive, preflight_delay=0.5, delay=1.5, post_install=True)
        key, public = keypair(work)
        signature = sign(archive, key)
        design_buffer = io.BytesIO()
        with zipfile.ZipFile(design_buffer, 'w') as z:
            z.writestr('dummy.txt', 'Synthetic transport smoke only.\n')
        design = design_buffer.getvalue()
        env['CF_V2_EVALUATOR_PUBLIC_KEY'] = public
        # Reuse the prebuilt gateway image with THIS checkout's gateway code mounted read-only.
        # An ephemeral port and unique project avoid the validator's running v1 services.
        override = work / 'compose.yaml'
        quote = json.dumps
        override.write_text(f'''services:
  v2-evaluator:
    image: {quote(args.image)}
    pull_policy: never
    volumes:
      - {quote(str(work / 'incoming') + ':/incoming')}
  eda-gateway:
    image: {quote(args.gateway_image)}
    pull_policy: never
    ports: !override ["127.0.0.1:0:8080"]
    volumes:
      - {quote(str(repo / 'gateway/main.py') + ':/app/main.py:ro')}
      - {quote(str(repo / 'gateway/v2_proxy.py') + ':/app/v2_proxy.py:ro')}
      - {quote(str(work / 'shared') + ':/shared')}
      - {quote(str(work / 'results') + ':/app/results')}
''')
        command = ['docker', 'compose', '-p', project, '-f', str(repo / 'docker-compose.yml'),
                   '-f', str(override), '--profile', 'v2']
        subprocess.run(command + ['up', '-d', '--no-build', '--pull', 'never', '--no-deps',
                                 'v2-evaluator', 'eda-gateway'], env=env, check=True)
        port = int(subprocess.check_output(command + ['port', 'eda-gateway', '8080'], env=env, text=True).strip().rsplit(':', 1)[1])

        def request(method, path, fields=None):
            body, headers = None, {}
            if fields is not None:
                body, headers['Content-Type'] = form(fields)
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
            finally:
                connection.close()

        deadline = time.monotonic() + 60
        while True:
            try:
                if request('GET', '/health')[0] == 200 and request('GET', '/v2/evaluator')[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, 'gateway startup timeout'
            time.sleep(0.1)
        status, headers, body = request('POST', '/v2/evaluate', dict(design_zip=design, evaluator_zip=archive.read_bytes(),
                       evaluator_sig=signature, evaluator_sha256=sha, request_id='compose-smoke'))
        assert status == 202, (status, body)
        assert headers['location'] == '/v2/status/compose-smoke'
        assert headers['x-cf-v2-allow-unsigned'] == '0'
        phases = {json.loads(body)['phase']}
        deadline = time.monotonic() + 90
        while True:
            status, headers, body = request('GET', '/v2/status/compose-smoke')
            phases.add(headers.get('x-cf-phase'))
            if status == 200: break
            assert status == 202 and time.monotonic() < deadline, (status, body)
            time.sleep(0.05)
        assert body == RAW, body
        assert phases == {'installing_evaluator', 'queued', 'running', 'done'}, phases
        assert headers['x-cf-evaluator-unsigned'] == '0'
        assert headers['x-cf-evaluator-sha256'] == sha
        assert request('GET', '/v2/health')[0] == 200
        assert request('POST', '/v2/evaluate', dict(design_zip=design, evaluator_sha256=sha, request_id='compose-smoke'))[2] == RAW
        salt = work / 'secrets/evaluator-salt.bin'
        # stat is allowed without reading the root-owned secret's bytes.
        assert salt.stat().st_mode & 0o777 == 0o600 and salt.stat().st_size == 64
        salt_inode = salt.stat().st_ino
        subprocess.run(command + ['restart', 'v2-evaluator'], env=env, check=True)
        deadline = time.monotonic() + 60
        while True:
            status, _, _ = request('GET', '/v2/health')
            if status == 200: break
            assert time.monotonic() < deadline, 'evaluator restart timeout'
            time.sleep(0.1)
        assert request('GET', '/v2/status/compose-smoke')[2] == RAW
        assert salt.stat().st_ino == salt_inode
        assert (work / 'data/preflight-count').read_text() == '1'
        print(json.dumps(dict(result='CONTAINER_SMOKE_PASS', signed=True, evaluator_sha256=sha,
                              phases=sorted(phases), restart=True,
                              body_sha256=__import__('hashlib').sha256(body).hexdigest())))
    finally:
        if command:
            subprocess.run(command + ['down', '--timeout', '30'], env=env, check=True)
        # Container-created directories are root-owned. Grant unlink access only inside
        # this unique task-owned scratch tree, then remove it and its temporary signing key.
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3',
                        '-v', str(work) + ':/cleanup', args.image, '-c',
                        "from pathlib import Path; p=Path('/cleanup'); "
                        "[x.chmod(0o777) for x in p.rglob('*') if x.is_dir() and not x.is_symlink()]; p.chmod(0o777)"], check=True)
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
