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
    # The DRAM model library is linked into, and loaded by, simulators built in sibling RTL boxes:
    # it must be a same-path bind source, so the provisioned copy under tools/ is the one used.
    if (root / 'tools/platform').is_dir():
        env.setdefault('CF_DRAM_ROOT', str(root / 'tools/platform'))
    elif Path('/opt/cf/platform').is_dir():
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
        want = digest(versions) + ':c6-runtime-2'
        marker = tools / '.cf-image-sha256'
        if marker.is_file() and marker.read_text() == want:
            return
        # The loader owns tools/; install before starting any evaluator service.
        temp = Path(tempfile.mkdtemp(prefix='tools-', dir=root / 'tmp'))
        temp.chmod(0o755)
        try:
            for src, dest in (('/usr/local/share/verilator', 'verilator/share'),
                              ('/usr/local/share/yosys', 'yosys/share'),
                              ('/opt/cf/pdk/nangate45', 'nangate45'),
                              ('/opt/cf/platform', 'platform')):
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


LOG_HEAD_BYTES = 32 * 1024
LOG_TAIL_BYTES = 64 * 1024
LOG_SIGNAL_BYTES = 128 * 1024   # lines between head and tail that signal failure
LOG_LINE_BYTES = 2048           # one line is cut to this
SUMMARY_LINES = 12
# What preflight, post_install steps and the tools under them print when something is wrong.
SIGNAL = re.compile(rb'FAIL|ERROR|Traceback|Exception|Assertion|Segmentation|Killed|Timeout|timed out|'
                    rb'exit(?:ed)?(?: with)?(?: status| code)?[ =:]+[1-9]|returncode[ =:]+-?[1-9]|'
                    rb'"(?:ok|passed|pass|success)"\s*:\s*false|not found|No such file|Permission denied|'
                    rb'fatal|refused|mismatch|'
                    # preflight.py: a failed check prints as MISSING (text) or "status": "MISSING" (--json)
                    rb'(?-i:\bMISSING\b)', re.I)


def _clip(line):
    return line if len(line) <= LOG_LINE_BYTES else line[:LOG_LINE_BYTES] + b' [line cut]\n'


def trim_log(path, step='', code=None):
    """Bound a failed step's log by total size, never by "last N lines": keep the head, the tail and, between
    them, every line that signals failure (so a check that scrolled out under later PASS lines is still
    there). Append a one-line loader summary naming the step, the exit code and the failure lines found.
    Written to a temporary file and moved over the log; mode 0600."""
    path = str(path)
    try:
        head, signal_lines, tail = [], [], []
        head_n = tail_n = signal_n = 0
        omitted = dropped = 0
        found = []
        last = b''
        prev, keep_next = None, False   # one line of context each side of a kept line (--json: the "check" name)

        def keep(line):
            nonlocal signal_n, dropped
            if signal_n + len(line) <= LOG_SIGNAL_BYTES:
                signal_lines.append(line); signal_n += len(line)
                return True
            dropped += 1
            return False

        with open(path, 'rb') as f:
            for raw in f:
                line = _clip(raw if raw.endswith(b'\n') else raw + b'\n')
                if SIGNAL.search(line):
                    # The line before names what failed (preflight --json: "check": ... precedes "status").
                    found.append((last.strip() + b' ' if last.strip().startswith(b'"check"') else b'') + line.strip())
                    if len(found) > 4 * SUMMARY_LINES:
                        del found[SUMMARY_LINES:-SUMMARY_LINES]
                last = line
                if head_n < LOG_HEAD_BYTES:
                    head.append(line); head_n += len(line)
                    continue
                tail.append(line); tail_n += len(line)
                while tail_n > LOG_TAIL_BYTES:
                    old = tail.pop(0); tail_n -= len(old)
                    if SIGNAL.search(old):
                        if prev is not None and keep(prev):
                            omitted -= 1
                        prev = None
                        keep(old)
                        keep_next = True
                    elif keep_next:
                        keep(old)
                        keep_next = False
                    else:
                        prev = old
                        omitted += 1
        out = list(head)
        if signal_lines or omitted or dropped:
            out.append(f'[loader] ... {omitted} routine lines omitted; failure-signalling lines kept below'
                       f'{f" ({dropped} more not kept)" if dropped else ""} ...\n'.encode())
            out += signal_lines
            out.append(b'[loader] ... end of omitted section ...\n')
        out += tail
        shown = found[:SUMMARY_LINES // 2] + found[-(SUMMARY_LINES - SUMMARY_LINES // 2):] if len(found) > SUMMARY_LINES else found
        text = b' | '.join(x[:300] for x in dict.fromkeys(shown))
        out.append(f'[loader] summary: step={step or Path(path).name} exit={code if code is not None else "unknown"} '
                   f'failed_checks={len(found)}: '.encode() + (text or b'none matched') + b'\n')
        tmp = path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(b''.join(out))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


class Installer:
    def __init__(self, root, salt, public_key='', allow_unsigned=False, modes=('T2',), allow_runc=False,
                 min_disk_gib=None):
        self.root, self.salt = Path(root), Path(salt)
        # None = the evaluator's own preflight minimum. An explicit operator override is passed as
        # --min-disk-gib, bound into the ready marker and reported by health.
        self.min_disk_gib = min_disk_gib
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
                    and data['modes'] == list(self.modes) and data['allow_runc'] == self.allow_runc
                    and data.get('min_disk_gib') == self.min_disk_gib else None)
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
                for i, step in enumerate(steps):
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
                    self.run(command, target, env, timeout, 'POST_INSTALL_FAILED',
                             self.log_path(sha, f"post_install-{i}"))
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
                    if self.min_disk_gib is not None:
                        command += ['--min-disk-gib', str(self.min_disk_gib)]
                    self.run(command, target, env, 300, 'EVALUATOR_PREFLIGHT_FAILED',
                             self.log_path(sha, f'preflight-{mode}'))
                ready = dict(sha256=sha, unsigned=unsigned, trust=self.trust(), modes=list(self.modes),
                             allow_runc=self.allow_runc, min_disk_gib=self.min_disk_gib, manifest=manifest)
                atomic_json(target / '.cf-ready.json', ready)
                readonly(target)
                sync_dir(target.parent)
                return ready
            except BaseException:
                remove_tree(target)
                raise
            finally:
                remove_tree(stage)

    def failure_logs(self, sha):
        """Paths of the (trimmed) step logs left by failed install steps of `sha`, newest first."""
        d = self.root / 'logs' / 'install'
        return sorted(d.glob(f'{sha}.*.log'), key=lambda p: p.stat().st_mtime, reverse=True)

    def log_path(self, sha, step):
        """Operator-only install log: <root>/logs/install/<sha>.<step>.log, 0600 in a 0700 directory."""
        d = self.root / 'logs' / 'install'
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
        return d / f'{sha}.{step}.log'

    @staticmethod
    def run(command, cwd, env, timeout, code, log):
        """Run one install step. Its output goes to `log` (operator-only); on failure the log is cut to its
        bounded by size (head, tail and every failure-signalling line, plus a summary), on success it is removed. The caller only ever sees `code`."""
        proc = None
        failed = True
        status = 'timeout/spawn-error'
        # Evaluator code is authenticated but may print private provisioning details: its output goes
        # to an operator-only log under the data root, never to the HTTP caller (which sees the code).
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)     # O_CREAT's mode does not apply to a log left by an earlier attempt.
        try:
            with os.fdopen(fd, 'wb') as out:
                proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
                status = proc.wait(timeout=timeout)
                failed = bool(status)
        except (OSError, subprocess.TimeoutExpired):
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        finally:
            if failed:
                trim_log(log, Path(log).stem.split('.', 1)[-1], status)
            else:
                Path(log).unlink(missing_ok=True)
        if failed:
            raise InstallError(code)
