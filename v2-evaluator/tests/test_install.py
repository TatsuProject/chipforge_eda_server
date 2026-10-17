from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import stat
import threading
import zipfile

import pytest

from loader.install import Installer
from loader.security import InstallError, digest, extract, fetch_asset, salt_once, verify
from fixtures import build_zip, keypair, sign


def prepare(tmp_path, **options):
    root = tmp_path / 'root'
    installer = Installer(root, tmp_path / 'secrets/salt', allow_unsigned=True)
    archive = tmp_path / 'fake.zip'
    sha = build_zip(archive, **options)
    archive.rename(root / 'archives' / (sha + '.zip'))
    return installer, sha


def test_signature_hash_and_unsigned_refused(tmp_path):
    archive = tmp_path / 'fake.zip'
    sha = build_zip(archive)
    key, public = keypair(tmp_path)
    signature = sign(archive, key)
    assert verify(archive, sha, signature, public, False) is False
    for expected, wrong_sha, sig, pub, unsigned in [
        ('EVALUATOR_HASH_MISMATCH', '0'*64, signature, public, False),
        ('EVALUATOR_SIGNATURE_REQUIRED', sha, b'', public, False),
        ('EVALUATOR_SIGNATURE_INVALID', sha, b'bad', public, True),
        ('EVALUATOR_PUBLIC_KEY_REQUIRED', sha, signature, '', False),
    ]:
        with pytest.raises(InstallError, match=expected):
            verify(archive, wrong_sha, sig, pub, unsigned)
    assert verify(archive, sha, b'', '', True) is True
    other = tmp_path / 'other'; other.mkdir()
    _, wrong_public = keypair(other)
    with pytest.raises(InstallError, match='EVALUATOR_SIGNATURE_INVALID'):
        verify(archive, sha, signature, wrong_public, False)
    archive.write_bytes(archive.read_bytes() + b'x')
    with pytest.raises(InstallError, match='EVALUATOR_SIGNATURE_INVALID'):
        verify(archive, digest(archive), signature, public, False)


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'v2/../../escape', 'v2\\evil', 'x/./y', 'x//y', 'C:/evil', '.cf-ready.json'])
def test_extraction_traversal_refused(tmp_path, name):
    archive, target = tmp_path / 'bad.zip', tmp_path / 'tree'
    target.mkdir()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, 'bad')
    with pytest.raises(InstallError):
        extract(archive, target)
    assert not list(target.iterdir())


@pytest.mark.parametrize('kind', [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO, stat.S_IFBLK])
def test_extraction_links_special_files_refused(tmp_path, kind):
    archive, target = tmp_path / 'bad.zip', tmp_path / 'tree'
    target.mkdir()
    with zipfile.ZipFile(archive, 'w') as z:
        info = zipfile.ZipInfo('v2/evil')
        info.create_system = 3
        info.external_attr = (kind | 0o777) << 16
        z.writestr(info, '../escape')
    with pytest.raises(InstallError, match='UNSAFE_ARCHIVE'):
        extract(archive, target)


def test_archive_size_and_count_limits(tmp_path):
    archive, target = tmp_path / 'big.zip', tmp_path / 'tree'
    target.mkdir()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('a', b'x'*10000)
        z.writestr('b', b'x'*10000)
    with pytest.raises(InstallError, match='EVALUATOR_TOO_LARGE'):
        extract(archive, target, max_bytes=15000)
    with pytest.raises(InstallError, match='EVALUATOR_TOO_MANY_FILES'):
        extract(archive, target, max_files=1)


def test_salt_once_concurrent_mode_and_location(tmp_path):
    root, salt = tmp_path / 'root', tmp_path / 'secrets/salt'
    root.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: salt_once(salt, root), range(16)))
    original = salt.read_bytes()
    assert len(original) == 64 and stat.S_IMODE(salt.stat().st_mode) == 0o600
    salt_once(salt, root)
    assert salt.read_bytes() == original
    with pytest.raises(InstallError, match='UNSAFE_SALT_LOCATION'):
        salt_once(root / 'bad', root)
    (tmp_path / 'alias').symlink_to(salt)
    with pytest.raises(InstallError, match='UNSAFE_SALT_LOCATION'):
        salt_once(tmp_path / 'alias', root)


@pytest.fixture
def asset_server():
    data = b'public synthetic asset\x00' * 10000
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            offset = int(self.headers['Range'][6:-1]) if self.headers.get('Range') else 0
            requests.append(offset)
            # /ignore exercises servers that do not implement Range.
            if self.path == '/ignore': offset = 0
            self.send_response(206 if offset else 200)
            if offset: self.send_header('Content-Range', f'bytes {offset}-{len(data)-1}/{len(data)}')
            self.send_header('Content-Length', str(len(data)-offset))
            self.end_headers(); self.wfile.write(data[offset:])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{server.server_port}', data, requests
    server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('endpoint', ['/', '/ignore'])
def test_asset_fetch_resume_cache_and_hash(tmp_path, asset_server, endpoint):
    url, data, requests = asset_server
    sha = hashlib.sha256(data).hexdigest()
    (tmp_path / (sha + '.part')).write_bytes(data[:777])
    asset = dict(url=url+endpoint, sha256=sha, size=len(data))
    dest = fetch_asset(tmp_path, asset)
    assert dest.read_bytes() == data and requests == [777]
    fetch_asset(tmp_path, asset)
    assert requests == [777]
    with pytest.raises(InstallError, match='ASSET_HASH_MISMATCH'):
        fetch_asset(tmp_path, dict(url=url, sha256='0'*64))
    with pytest.raises(InstallError, match='ASSET_TOO_LARGE'):
        fetch_asset(tmp_path, dict(url=url, sha256='1'*64, size=10))
    with pytest.raises(InstallError, match='INVALID_ASSET_URL'):
        fetch_asset(tmp_path, dict(url='file:///etc/passwd', sha256=sha))


def test_full_install_public_asset_post_install_and_readonly(tmp_path, asset_server):
    url, data, _ = asset_server
    asset = dict(url=url, sha256=hashlib.sha256(data).hexdigest(), target='data/public', size=len(data))
    installer, sha = prepare(tmp_path, assets=[asset], post_install=True)
    phases = []
    ready = installer.install(sha, b'', phases.append)
    assert ready['unsigned'] is True and phases[-1] == 'preflight'
    assert (installer.root / 'data/public').read_bytes() == data
    assert (installer.root / 'data/regenerated').read_bytes() == b'synthetic generated data'
    tree = installer.root / 'evaluators' / sha
    assert tree.stat().st_mode & 0o222 == 0
    assert (tree / 'v2/deploy/service.py').stat().st_mode & 0o222 == 0
    assert installer.ready(sha)
    installer.allow_unsigned = False
    assert installer.ready(sha) is None


def test_operator_disk_minimum_passed_to_preflight_and_bound(tmp_path):
    installer, sha = prepare(tmp_path)
    installer.min_disk_gib = 20
    ready = installer.install(sha, b'')
    assert ready['min_disk_gib'] == 20
    assert '--min-disk-gib 20' in (installer.root / 'preflight-argv').read_text()
    assert installer.ready(sha)
    installer.min_disk_gib = None      # back to the evaluator default: preflight must run again
    assert installer.ready(sha) is None


def test_default_disk_minimum_left_to_the_evaluator(tmp_path):
    installer, sha = prepare(tmp_path)
    assert installer.install(sha, b'')['min_disk_gib'] is None
    assert '--min-disk-gib' not in (installer.root / 'preflight-argv').read_text()


def test_concurrent_install_single_preflight_and_failed_retry(tmp_path):
    installer, sha = prepare(tmp_path, preflight_delay=0.1, fail_preflight=True)
    with pytest.raises(InstallError, match='EVALUATOR_PREFLIGHT_FAILED'):
        installer.install(sha, b'')
    assert not installer.ready(sha)
    (installer.root / 'permit-preflight').touch()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: installer.install(sha, b''), range(6)))
    assert all(r['sha256'] == sha for r in results)
    assert (installer.root / 'preflight-count').read_text() == '2'


def test_asset_target_symlink_and_reserved_refused(tmp_path, asset_server):
    url, data, _ = asset_server
    asset = dict(url=url, sha256=hashlib.sha256(data).hexdigest(), target='tools/evil')
    installer, sha = prepare(tmp_path, assets=[asset])
    with pytest.raises(InstallError, match='UNSAFE_ASSET_TARGET'):
        installer.install(sha, b'')
    (installer.root / 'data').symlink_to(tmp_path)
    from loader.install import public_target
    with pytest.raises(InstallError, match='UNSAFE_PATH'):
        public_target(installer.root, 'data/escape')


def test_image_tools_provisioned_for_sibling_runtime(tmp_path):
    if not Path('/opt/cf/VERSIONS.txt').exists():
        pytest.skip('image collateral check runs inside the evaluator image')
    import subprocess
    from loader.install import provision_tools
    (tmp_path / 'tmp').mkdir()
    provision_tools(tmp_path)
    runtime = tmp_path / 'tools/verilator/runtime'
    for name in ('libc.so.6', 'libm.so.6', 'libstdc++.so.6', 'libgcc_s.so.1'):
        assert (runtime / 'lib' / name).is_file()
    result = subprocess.run([str(runtime / 'ld.so'), '--library-path', str(runtime / 'lib'),
                             str(runtime / 'verilator_bin'), '--version'], capture_output=True)
    assert result.returncode == 0 and b'Verilator' in result.stdout
    # Simulators built in sibling RTL boxes link and load the DRAM model: same-path copy, used by env.
    from loader.install import environment
    assert list((tmp_path / 'tools/platform').glob('DRAMsim3-*/libdramsim3.so'))
    assert environment(tmp_path, 'a' * 64, tmp_path / 'salt')['CF_DRAM_ROOT'] == str(tmp_path / 'tools/platform')


def test_minimal_layout_compatibility(tmp_path):
    installer, sha = prepare(tmp_path, c7=False, post_install=True)
    assert installer.install(sha, b'')['manifest']['schema'] == 'cf-evaluator/1'


@pytest.mark.parametrize('change', ['bytes', 'inventory'])
def test_c7_file_inventory_verified_before_execution(tmp_path, change):
    import json
    installer, sha = prepare(tmp_path)
    archive = installer.root / 'archives' / (sha + '.zip')
    with zipfile.ZipFile(archive) as z:
        files = {name: z.read(name) for name in z.namelist()}
    if change == 'bytes':
        files['v2/deploy/service.py'] += b'\n# changed\n'
    else:
        manifest = json.loads(files['MANIFEST.json'])
        del manifest['files']['v2/deploy/service.py']
        files['MANIFEST.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, 'w') as z:
        for name, value in files.items(): z.writestr(name, value)
    new_sha = digest(archive)
    archive.rename(archive.with_name(new_sha + '.zip'))
    code = 'EVALUATOR_FILE_HASH_MISMATCH' if change == 'bytes' else 'EVALUATOR_FILE_INVENTORY_MISMATCH'
    with pytest.raises(InstallError, match=code): installer.install(new_sha, b'')
    assert not (installer.root / 'preflight-count').exists()


def test_c7_archive_assets_download_and_share_destination(tmp_path):
    import io
    blobs = {}
    for name, value in (('first', b'one'), ('second', b'two')):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr(name, value)
            z.writestr('common', b'identical shared file')
        blobs['/' + name] = data.getvalue()
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', str(len(blobs[self.path])))
            self.end_headers(); self.wfile.write(blobs[self.path])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        assets = [dict(url=f'http://127.0.0.1:{server.server_port}{name}', sha256=hashlib.sha256(data).hexdigest(),
                       size=len(data), target='data/unpacked', unpack='zip') for name, data in blobs.items()]
        installer, sha = prepare(tmp_path, assets=assets)
        installer.install(sha, b'')
        assert (installer.root/'data/unpacked/first').read_bytes() == b'one'
        assert (installer.root/'data/unpacked/second').read_bytes() == b'two'
        assert (installer.root/'data/unpacked/common').read_bytes() == b'identical shared file'
        assert seen == ['/first', '/second']
        installer.install(sha, b'')
        assert seen == ['/first', '/second']
    finally:
        server.shutdown(); server.server_close(); thread.join()
