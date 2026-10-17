"""One authenticated, atomic evaluator installation. No baseline run at install."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

from .security import (InstallError, SHA, atomic_json, digest, extract, fetch_asset, file_lock,
                       inside, read_json, readonly, relative, remove_tree, salt_once, sync_dir, verify)
from . import contract
from .assets import materialize

RESERVED = {'evaluators', 'loader', 'archives', 'tools', 'state', 'escrow', 'locks', 'tmp', 'assets', 'lanes'}


def public_target(root, name):
    p = relative(name)
    if p.parts[0] in RESERVED or p.parts[0].startswith('.'):
        raise InstallError('UNSAFE_ASSET_TARGET')
    return inside(root, name)


def interpreter():
    pinned = Path('/opt/cf/venv/orchestrator/bin/python')
    return str(pinned) if pinned.is_file() else sys.executable


def environment(root, sha, salt):
    state, posix = root / 'state' / sha, root / 'escrow' / sha
    for p in (state, posix, root / 'tmp', root / 'lanes', root / 'locks'):
        p.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CF_V2_ROOT=str(root), CF_EVALUATOR_SALT_FILE=str(salt),
               CF_ORCH_SSD=str(state), CF_ORCH_POSIX=str(posix), TMPDIR=str(root / 'tmp'),
               CF_BUILD_TMPDIR=str(root / 'tmp'), CF_OUTSIDE_KILL_TMP=str(root / 'tmp'),
               CF_BUILD_CACHE_DIR=str(root / 'cache/compiler-builds'),
               CF_PLATFORM_SIM_CACHE=str(root / 'cache/platform-sim'),
               CF_DOCKER_RATE_FILE=str(root / 'locks/docker-rate.lock'), CF_DOCKER_REQUIRED_SERVICES='',
               CF_SCOPE_BACKEND='cfscope', PYTHONDONTWRITEBYTECODE='1')
    if Path('/opt/cf/platform').is_dir():
        env.setdefault('CF_DRAM_ROOT', '/opt/cf/platform')
    if Path('/opt/cf/cacti/cacti').is_file():
        env.setdefault('CF_CACTI', '/opt/cf/cacti/cacti')
    return env


def provision_tools(root):
    """Image-only tools can stay in /opt; bind sources must exist at the host's identical path."""
    versions = Path('/opt/cf/VERSIONS.txt')
    if not versions.is_file():
        return  # Source-only unit tests have no tools image.
    tools = root / 'tools'
    with file_lock(root / 'locks/tools.lock'):
        want = digest(versions) + ':c6-runtime-1'
        marker = tools / '.cf-image-sha256'
        if marker.is_file() and marker.read_text() == want:
            return
        # The loader owns tools/; install before starting any evaluator service.
        temp = Path(tempfile.mkdtemp(prefix='tools-', dir=root / 'tmp'))
        temp.chmod(0o755)
        try:
            for src, dest in (('/usr/local/share/verilator', 'verilator/share'),
                              ('/usr/local/share/yosys', 'yosys/share'),
                              ('/opt/cf/pdk/nangate45', 'nangate45')):
                shutil.copytree(src, temp / dest, symlinks=False)
            binary = Path('/usr/local/bin/verilator_bin')
            runtime = temp / 'verilator/runtime'
            (runtime / 'lib').mkdir(parents=True)
            shutil.copy2(binary, runtime / 'verilator_bin')
            result = subprocess.run(['ldd', str(binary)], capture_output=True, text=True, check=True)
            for lib in set(re.findall(r'(/[^\s()]+)', result.stdout)):
                source = Path(lib)
                if source.is_file():
                    dest = runtime / 'ld.so' if source.name.startswith('ld-linux') else runtime / 'lib' / source.name
                    shutil.copy2(source, dest)
            # Verilator may statically link these, while generated simulations and
            # the evaluator's generic native runtime still require both shared libraries.
            for library in ('libstdc++.so.6', 'libgcc_s.so.1'):
                source = Path(subprocess.check_output(['g++', '-print-file-name=' + library], text=True).strip())
                if not source.is_absolute() or not source.is_file():
                    raise InstallError('IMAGE_RUNTIME_LIBRARY_MISSING')
                shutil.copy2(source, runtime / 'lib' / library)
            (temp / '.cf-image-sha256').write_text(want)
            if tools.exists():
                remove_tree(tools)
            os.replace(temp, tools)
            sync_dir(root)
        finally:
            remove_tree(temp)


class Installer:
    def __init__(self, root, salt, public_key='', allow_unsigned=False, modes=('T2',), allow_runc=False):
        self.root, self.salt = Path(root), Path(salt)
        self.public_key, self.allow_unsigned = public_key.strip(), allow_unsigned
        self.modes, self.allow_runc = tuple(modes), allow_runc
        for name in ('evaluators', 'assets', 'archives', 'locks', 'tmp'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def trust(self):
        return hashlib.sha256(self.public_key.encode()).hexdigest()

    def ready(self, sha):
        marker = self.root / 'evaluators' / sha / '.cf-ready.json'
        try:
            data = read_json(marker)
            return (data if data['sha256'] == sha and data['trust'] == self.trust()
                    and (not data['unsigned'] or self.allow_unsigned)
                    and data['modes'] == list(self.modes) and data['allow_runc'] == self.allow_runc else None)
        except (InstallError, KeyError, TypeError):
            return None

    def install(self, sha, signature, phase=lambda _: None):
        if not SHA.fullmatch(sha):
            raise InstallError('INVALID_EVALUATOR_HASH')
        with file_lock(self.root / 'locks' / (sha + '.install.lock')):
            if ready := self.ready(sha):
                return ready
            archive = self.root / 'archives' / (sha + '.zip')
            phase('verify')
            unsigned = verify(archive, sha, signature, self.public_key, self.allow_unsigned)
            target = self.root / 'evaluators' / sha
            stage = Path(tempfile.mkdtemp(prefix=sha + '-', dir=self.root / 'evaluators'))
            try:
                phase('extract')
                extract(archive, stage)
                manifest = contract.manifest(read_json(stage / 'MANIFEST.json'), stage)
                for key, default in (('service', 'v2/deploy/service.py'), ('preflight', 'v2/deploy/preflight.py'),
                                     ('policy', 'evaluator/policy.json'), ('assets', 'assets.json')):
                    manifest.setdefault(key, default)
                    if not inside(stage, manifest[key]).is_file():
                        raise InstallError('MISSING_EVALUATOR_FILE')
                policy = read_json(stage / manifest['policy'])
                if not isinstance(policy, dict) or not set(self.modes) <= set(policy):
                    raise InstallError('EVALUATOR_MODE_UNAVAILABLE')
                # Post-install/preflight execute in the FINAL path: metadata may bind paths into receipts.
                remove_tree(target)
                os.replace(stage, target)
                phase('fetch_assets')
                assets = contract.assets(read_json(target / manifest['assets']))
                for asset in assets:
                    dest = public_target(self.root, asset.get('target'))
                    cached = fetch_asset(self.root / 'assets', asset)
                    materialize(self.root, cached, dest, asset['unpack'])
                phase('salt')
                salt_once(self.salt, self.root)
                env = environment(self.root, sha, self.salt)
                phase('post_install')
                steps = manifest.get('post_install', [])
                if not isinstance(steps, list) or len(steps) > 64:
                    raise InstallError('INVALID_POST_INSTALL')
                values = {'python': interpreter(), 'root': str(self.root), 'evaluator': str(target),
                          'assets': str(self.root / 'assets'), 'state': env['CF_ORCH_SSD'], 'posix': env['CF_ORCH_POSIX']}
                for step in steps:
                    if not isinstance(step, dict) or set(step) - {'argv', 'outputs', 'timeout_s'}:
                        raise InstallError('INVALID_POST_INSTALL')
                    argv, outputs = step.get('argv'), step.get('outputs')
                    timeout = step.get('timeout_s', 7200)
                    if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv)
                            or not isinstance(outputs, list) or (not outputs and manifest['schema'] == 'cf-evaluator/1')
                            or type(timeout) is not int or not 1 <= timeout <= 86400):
                        raise InstallError('INVALID_POST_INSTALL')
                    try:
                        command = [a.format_map(values) for a in argv]
                    except (KeyError, ValueError):
                        raise InstallError('INVALID_POST_INSTALL') from None
                    self.run(command, target, env, timeout, 'POST_INSTALL_FAILED')
                    for output in outputs:
                        if not isinstance(output, dict) or set(output) != {'path', 'sha256'} or not SHA.fullmatch(output['sha256']):
                            raise InstallError('INVALID_POST_INSTALL_OUTPUT')
                        p = public_target(self.root, output['path'])
                        if not p.is_file() or digest(p) != output['sha256']:
                            raise InstallError('POST_INSTALL_HASH_MISMATCH')
                phase('preflight')
                provision_tools(self.root)
                for mode in self.modes:
                    command = [interpreter(), '-B', str(target / manifest['preflight']), '--mode', mode,
                               '--scratch', env['CF_ORCH_SSD'], '--posix', env['CF_ORCH_POSIX'], '--json']
                    if self.allow_runc:
                        command.append('--allow-runc')
                    self.run(command, target, env, 300, 'EVALUATOR_PREFLIGHT_FAILED')
                ready = dict(sha256=sha, unsigned=unsigned, trust=self.trust(), modes=list(self.modes),
                             allow_runc=self.allow_runc, manifest=manifest)
                atomic_json(target / '.cf-ready.json', ready)
                readonly(target)
                sync_dir(target.parent)
                return ready
            except BaseException:
                remove_tree(target)
                raise
            finally:
                remove_tree(stage)

    @staticmethod
    def run(command, cwd, env, timeout, code):
        proc = None
        try:
            # Evaluator code is authenticated but may print private provisioning details.
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            if proc.wait(timeout=timeout):
                raise InstallError(code)
        except (OSError, subprocess.TimeoutExpired):
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise InstallError(code) from None
