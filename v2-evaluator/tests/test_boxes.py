"""v2.0.1: the loader removes its own leftover evaluator boxes, found by this root's label only.

No real Docker: a fake `docker` CLI on PATH keeps one file per container holding its labels.
"""
import os
from pathlib import Path
import sys

import pytest

from loader import boxes
from loader.supervisor import Service

FAKE = r'''#!{python}
import os, sys
from pathlib import Path
root = Path(os.environ["FAKE_DOCKER"])
with open(root.parent / "calls.log", "a") as f: f.write(" ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if os.environ.get("FAKE_DOCKER_DOWN"):
    sys.stderr.write("Cannot connect to the Docker daemon\n"); sys.exit(1)
if args[0] == "ps":
    want = [args[i + 1].split("=", 1)[1] for i, a in enumerate(args) if a == "--filter"]
    for box in sorted(root.iterdir()):
        if all(w in box.read_text().split(",") for w in want):
            print(box.name)
    sys.exit(0)
if args[:2] == ["rm", "-f"]:
    (root / args[2]).unlink(missing_ok=True); sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def docker(tmp_path, monkeypatch):
    store, bin_dir = tmp_path / 'docker/boxes', tmp_path / 'docker/bin'
    store.mkdir(parents=True)
    bin_dir.mkdir()
    (bin_dir / 'docker').write_text(FAKE.format(python=sys.executable))
    (bin_dir / 'docker').chmod(0o755)
    monkeypatch.setenv('PATH', f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv('FAKE_DOCKER', str(store))

    def put(name, *labels):
        (store / name).write_text(','.join(labels))
    put.names = lambda: sorted(p.name for p in store.iterdir())
    return put


def test_reap_removes_only_this_roots_labelled_boxes(tmp_path, docker):
    root = tmp_path / 'root'
    docker('ours-sim', boxes.label(root), 'cf.orch.v8.worker=w8-1')
    docker('ours-semantics', boxes.label(root))
    docker('other-root', boxes.label(tmp_path / 'another-root'))
    docker('cf-evaluator-lookalike', 'cf.orch.natural=1')       # evaluator-like name, no root label
    lines = []
    got = boxes.reap(root, 'test', log=lines.append)
    assert sorted(got['removed']) == ['ours-semantics', 'ours-sim'] and got['left'] == [] and got['verified']
    assert docker.names() == ['cf-evaluator-lookalike', 'other-root']
    assert lines == ['leftover evaluator boxes (test): 2 found, 2 removed']
    assert boxes.reap(root, 'again', log=lines.append)['found'] == [] and len(lines) == 1   # nothing found: silent


def test_reap_reports_an_unreachable_daemon_and_never_raises(tmp_path, docker, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_DOWN', '1')
    lines = []
    assert boxes.reap(tmp_path, 'test', log=lines.append) == dict(found=[], removed=[], left=[], verified=False)
    assert 'not verified' in lines[0]
    monkeypatch.setenv('PATH', str(tmp_path / 'no-docker-here'))
    assert boxes.reap(tmp_path, 'test', log=lines.append)['verified'] is False


def test_with_label_appends_to_inherited_labels(tmp_path):
    env = boxes.with_label({'CF_DOCKER_LABELS': 'site=a,', 'X': '1'}, tmp_path)
    assert env['CF_DOCKER_LABELS'] == 'site=a,' + boxes.label(tmp_path) and env['X'] == '1'
    assert boxes.with_label(env, tmp_path) == env
    assert boxes.label(tmp_path) != boxes.label(tmp_path / 'x')


class _Installer:
    def __init__(self, root):
        self.root, self.salt, self.allow_runc = root, root / 'salt', False

    def ready(self, sha):
        return {'manifest': {'service': 'service.py', 'policy': 'policy.json'}}


def test_service_start_and_stop_clean_up_leftovers(tmp_path, docker, monkeypatch):
    from loader import supervisor
    root = tmp_path / 'root'
    root.mkdir()
    service = Service(_Installer(root))
    docker('left-by-crash', boxes.label(root))
    seen = {}

    real = supervisor.subprocess.Popen

    def popen(command, *a, **kw):
        if command[0] == 'docker':            # the module is shared: Docker CLI calls go through
            return real(command, *a, **kw)
        seen.update(names=docker.names(), labels=kw['env']['CF_DOCKER_LABELS'])
        raise OSError('test: no service')
    monkeypatch.setattr(supervisor, 'environment', lambda *a: {'CF_DOCKER_LABELS': 'site=a', 'CF_ORCH_SSD': str(tmp_path), 'CF_ORCH_POSIX': 'p'})
    monkeypatch.setattr(supervisor.subprocess, 'Popen', popen)
    with pytest.raises(OSError):
        service.ensure('a' * 64)
    assert seen == {'names': [], 'labels': 'site=a,' + boxes.label(root)}   # cleaned BEFORE the start

    docker('left-by-dead-service', boxes.label(root))

    class Dead:
        pid, returncode = 2 ** 22 + 7, -9
        def poll(self): return -9
    service.proc = Dead()
    service.stop()
    assert docker.names() == []


def test_service_output_goes_to_its_log_and_an_exit_is_reported(tmp_path, docker, monkeypatch, capsys):
    """v2.0.1: the evaluator service's own output is kept (service.log beside workers.log), and the
    loader's log names a service that ended by itself and how (signal / return code)."""
    from loader import supervisor
    root = tmp_path / 'root'
    (root / 'evaluators' / ('a' * 64)).mkdir(parents=True)
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'service.log').write_bytes(b'x' * (supervisor.LOG_ROTATE_BYTES + 1))
    monkeypatch.setattr(supervisor, 'environment', lambda *a: {'CF_ORCH_SSD': str(state), 'CF_ORCH_POSIX': 'p'})
    monkeypatch.setattr(supervisor, 'interpreter', lambda: sys.executable)
    script = root / 'evaluators' / ('a' * 64) / 'service.py'
    script.write_text('import os, signal, sys\nprint("plan line", flush=True)\nprint("traceback", file=sys.stderr, flush=True)\n'
                      'os.kill(os.getpid(), signal.SIGKILL)\n')
    service = Service(_Installer(root))
    with pytest.raises(supervisor.InstallError):
        service.ensure('a' * 64)
    assert (state / 'service.log').read_text() == 'plan line\ntraceback\n'
    assert (state / 'service.log.1').stat().st_size == supervisor.LOG_ROTATE_BYTES + 1
    err = capsys.readouterr().err
    assert f"evaluator service {'a' * 12} pid " in err and 'exited during start-up: signal 9 (SIGKILL)' in err
    assert f"service log {state / 'service.log'}; worker log {state / 'workers.log'}" in err
    assert supervisor.exit_text(3) == 'returncode 3' and supervisor.exit_text(-15) == 'signal 15 (SIGTERM)'


def _hold(path):
    """A separate process (an evaluator that outlived its loader) holding flock on `path`."""
    import subprocess
    p = subprocess.Popen([sys.executable, '-c', 'import fcntl, os, time\n'
                          f'fd = os.open({str(path)!r}, os.O_RDONLY | os.O_CREAT, 0o666)\n'
                          "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(120)"],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == 'held'
    return p


@pytest.mark.parametrize('which', ['evaluation', 'state'])
def test_reap_unowned_removes_nothing_while_a_live_evaluator_holds_a_lock(tmp_path, docker, monkeypatch, which):
    """Codex review of v201 (P0): a box with this root's label may belong to a live evaluator (one that outlived a
    loader restart). Nothing is removed while any process holds the root's evaluation lock or the service's state
    lock; once released, leftovers go."""
    monkeypatch.delenv('CF_V2_HOST_LOCK', raising=False)
    root, state = tmp_path / 'root', tmp_path / 'state'
    root.mkdir()
    state.mkdir()
    docker('live-box', boxes.label(root))
    holder = _hold(root / boxes.EVALUATION_LOCK if which == 'evaluation' else state / 'http.lock')
    lines = []
    try:
        got = boxes.reap_unowned(root, 'test', state=state, log=lines.append)
        assert got['skipped'] and got['found'] == [] and docker.names() == ['live-box']
        assert 'not removed: a live evaluator holds' in lines[0]
    finally:
        holder.kill()
        holder.wait()
    assert boxes.reap_unowned(root, 'test', state=state, log=lines.append)['removed'] == ['live-box']
    assert docker.names() == []


def test_a_restarted_loader_never_reaps_a_running_evaluators_boxes(tmp_path, docker, monkeypatch):
    """A new loader (fresh Service, no process of its own) meets an evaluator that still owns its evaluation lock:
    ensure() removes nothing before it starts its own service."""
    from loader import supervisor
    monkeypatch.delenv('CF_V2_HOST_LOCK', raising=False)
    root = tmp_path / 'root'
    root.mkdir()
    docker('running-evaluation-box', boxes.label(root))
    real = supervisor.subprocess.Popen

    def popen(command, *a, **kw):
        if command[0] == 'docker':
            return real(command, *a, **kw)
        raise OSError('test: no service')
    holder = _hold(root / boxes.EVALUATION_LOCK)          # before Popen is patched (the module is shared)
    monkeypatch.setattr(supervisor, 'environment', lambda *a: {'CF_ORCH_SSD': str(tmp_path / 'state'), 'CF_ORCH_POSIX': 'p'})
    monkeypatch.setattr(supervisor.subprocess, 'Popen', popen)
    try:
        with pytest.raises(OSError):
            Service(_Installer(root)).ensure('a' * 64)
        assert docker.names() == ['running-evaluation-box']
    finally:
        holder.kill()
        holder.wait()
