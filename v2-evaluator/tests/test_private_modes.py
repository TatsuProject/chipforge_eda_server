"""The data root and the secrets directory are owner-only after loader start-up.

The evaluator writes box-readable files (umask 022): the uid-65534 sibling boxes read them through
bind mounts. Privacy from other local users is therefore the mode of the TOP of each private tree,
which the loader sets and verifies at start-up; nothing below it is chmodded recursively."""
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from loader.install import Installer
from loader.runtime import Runtime
from loader.security import InstallError, PrivateModeError, owner_only, salt_once


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def open_layout(tmp_path):
    """What a host provisioned by hand looks like: a group-writable root, a world-readable salt."""
    root, secrets = tmp_path / 'root', tmp_path / 'secrets'
    (root / 'state' / ('a' * 64) / 'runs').mkdir(parents=True)
    os.chmod(root, 0o775)
    secrets.mkdir(mode=0o755)
    os.chmod(secrets, 0o755)
    salt = secrets / 'evaluator-salt.bin'
    salt.write_bytes(os.urandom(64))
    os.chmod(salt, 0o644)
    private = root / 'state' / ('a' * 64) / 'runs' / 'hidden-input.bin'
    private.write_bytes(b'private tensor')
    os.chmod(private, 0o644)      # box-readable on purpose
    return root, secrets, salt, private


def test_startup_closes_the_data_root_and_the_secrets(tmp_path, umask_022):
    root, secrets, salt, private = open_layout(tmp_path)
    runtime = Runtime(root, salt)
    try:
        assert mode(root) == 0o700
        assert mode(secrets) == 0o700
        assert mode(salt) == 0o600
        # Not recursive: what the boxes read below the root keeps its box-readable modes.
        assert mode(root / 'state') == 0o755 and mode(private) == 0o644
    finally:
        runtime.close()


def test_startup_creates_a_missing_secrets_directory_owner_only(tmp_path, umask_022):
    runtime = Runtime(tmp_path / 'root', tmp_path / 'secrets' / 'salt')
    try:
        assert mode(tmp_path / 'root') == 0o700 and mode(tmp_path / 'secrets') == 0o700
    finally:
        runtime.close()


def test_another_uid_cannot_reach_a_private_file_below_the_root(tmp_path, umask_022):
    root, secrets, salt, private = open_layout(tmp_path)
    Runtime(root, salt).close()
    # The traversal rule: resolving root/... needs search (x) on root for the caller's class.
    # A uid that is not the owner and lacks CAP_DAC_* is in "group" or "other", and both are 0.
    assert mode(root) & 0o077 == 0 and mode(secrets) & 0o077 == 0
    if os.geteuid() != 0:
        pytest.skip('switching uid needs root; the real-uid read is covered by the box test')
    probe = ('import os,sys\nos.setgroups([]); os.setgid(65534); os.setuid(65534)\n'
             'try:\n open(sys.argv[1],"rb").read(); print("READ")\n'
             'except PermissionError: print("DENIED")\n')
    for target in (private, salt):
        out = subprocess.run([sys.executable, '-I', '-c', probe, str(target)], capture_output=True, text=True)
        assert out.stdout.strip() == 'DENIED', out


def test_install_logs_are_0600_even_when_a_previous_log_was_open(tmp_path, umask_022):
    installer = Installer(tmp_path / 'root', tmp_path / 'secrets' / 'salt')
    log = installer.log_path('b' * 64, 'preflight')
    log.write_text('left by an earlier attempt')
    os.chmod(log, 0o644)
    with pytest.raises(InstallError):
        Installer.run(['false'], tmp_path, dict(os.environ), 10, 'PREFLIGHT_FAILED', str(log))
    assert mode(log) == 0o600
    assert mode(log.parent) == 0o700 and mode(log.parent.parent) == 0o700


def test_salt_once_keeps_the_salt_0600_in_a_0700_directory(tmp_path, umask_022):
    secrets = tmp_path / 'secrets'
    secrets.mkdir(mode=0o755)
    os.chmod(secrets, 0o755)
    salt_once(secrets / 'salt', tmp_path / 'root')
    assert mode(secrets) == 0o700 and mode(secrets / 'salt') == 0o600


def test_a_root_whose_mode_cannot_be_closed_refuses_to_start(tmp_path, umask_022, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    os.chmod(root, 0o755)
    monkeypatch.setattr(os, 'chmod', lambda *a, **k: None)     # NTFS/FUSE: chmod is ignored
    with pytest.raises(PrivateModeError, match='0755'):
        Runtime(root, tmp_path / 'secrets' / 'salt')


def test_owner_only_never_names_the_secrets_location(tmp_path, monkeypatch):
    secrets = tmp_path / 'very-private-place'
    secrets.mkdir()
    os.chmod(secrets, 0o755)
    monkeypatch.setattr(os, 'chmod', lambda *a, **k: None)
    with pytest.raises(PrivateModeError) as err:
        owner_only(secrets, 0o700, label='the evaluator secrets directory')
    assert 'very-private-place' not in str(err.value)


def test_running_as_root_the_loader_takes_ownership_of_an_operator_created_root(tmp_path, umask_022, monkeypatch):
    """An evaluator preflight probes the root from a box as uid 0 with no capabilities: that is
    "other" to an operator-owned 0700 root, so the loader (root) owns it."""
    root = tmp_path / 'root'
    root.mkdir()
    calls = []
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'getegid', lambda: 0)
    monkeypatch.setattr(os, 'chown', lambda path, uid, gid: calls.append((Path(path), uid, gid)))
    Runtime(root, tmp_path / 'secrets' / 'salt').close()
    assert calls == [(root, 0, 0)] and mode(root) == 0o700
