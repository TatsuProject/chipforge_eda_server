"""Gateway housekeeping: EDA version, free disk, deleting old evaluator versions. Run: pytest gateway/tests"""
import asyncio, os, sqlite3, sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import housekeeping  # noqa: E402

A, B, C, D, E, F = (c * 64 for c in "abcdef")


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    (r / "loader").mkdir(parents=True)
    db = sqlite3.connect(r / "loader" / "http.sqlite")
    db.executescript("""CREATE TABLE installs (sha TEXT, attempt INTEGER, phase TEXT, error TEXT, unsigned INTEGER);
                        CREATE TABLE jobs (request_id TEXT, sha TEXT, phase TEXT);""")
    db.commit(); db.close()
    monkeypatch.setenv("CF_V2_ROOT", str(r))
    monkeypatch.delenv("CF_V2_KEEP_OLD_EVALUATORS", raising=False)
    housekeeping.last_cleanup = None
    return r


def sql(root, q, *args):
    db = sqlite3.connect(root / "loader" / "http.sqlite"); db.execute(q, args); db.commit(); db.close()


def version(root, sha, big=0):
    for d in ("state", "escrow", "evaluators"):
        (root / d / sha).mkdir(parents=True, exist_ok=True)
    (root / "state" / sha / "runs.bin").write_bytes(b"x" * big)
    (root / "evaluators" / sha).chmod(0o555)                  # the installer makes it read-only
    (root / "archives").mkdir(exist_ok=True)
    (root / "archives" / (sha + ".zip")).write_bytes(b"zip")


def present(root, sha):
    return [d for d in ("state", "escrow", "evaluators") if (root / d / sha).exists()]


def run(coro):
    return asyncio.run(coro)


def test_only_versions_nothing_needs_are_deleted(root):
    for sha in (A, B, C, D, E, F):
        version(root, sha, big=1 << 20 if sha == A else 0)
    sql(root, "INSERT INTO installs VALUES(?,1,'ready',NULL,0)", A)      # old, ready, unused: goes
    sql(root, "INSERT INTO installs VALUES(?,1,'ready',NULL,0)", B)      # runs a pending job: stays
    sql(root, "INSERT INTO jobs VALUES('r1',?,'running')", B)
    sql(root, "INSERT INTO installs VALUES(?,1,'preflight',NULL,0)", C)  # being installed: stays
    sql(root, "INSERT INTO installs VALUES(?,1,'ready',NULL,0)", D)      # the service runs it: stays
    sql(root, "INSERT INTO installs VALUES(?,1,'ready',NULL,0)", E)      # newest ready: stays
    # F: only leftovers (a failed install): goes
    done = run(housekeeping.prune("periodic", D))
    assert done["evaluators"] == [A, F] and done["reason"] == "periodic"
    assert present(root, A) == [] and present(root, F) == []
    for sha in (B, C, D, E):
        assert present(root, sha) == ["state", "escrow", "evaluators"]
    assert (root / "archives" / (A + ".zip")).is_file()                  # kept for a reinstall
    assert list((root / "tmp" / "cf-old-evaluators").iterdir()) == []   # emptied
    h = housekeeping.decorate({"ready": True})
    assert h["ready"] and h["eda_version"] == housekeeping.VERSION and h["last_cleanup"]["evaluators"] == [A, F]
    assert h["disk"]["evaluator_versions"] == 4 and h["disk"]["free_gib"] > 0


def test_nothing_is_deleted_when_the_evaluator_is_unreachable_or_nothing_is_installed(root):
    version(root, A)
    assert run(housekeeping.prune("periodic", False)) is None             # service unknown
    assert run(housekeeping.prune("periodic", None)) is None              # nothing ready, no job, no install
    assert present(root, A) == ["state", "escrow", "evaluators"]


def test_switch_off_and_no_loader_database(root, monkeypatch):
    version(root, A); version(root, B)
    sql(root, "INSERT INTO installs VALUES(?,1,'ready',NULL,0)", B)
    monkeypatch.setenv("CF_V2_KEEP_OLD_EVALUATORS", "1")
    assert run(housekeeping.prune("periodic", B)) is None and present(root, A)
    monkeypatch.delenv("CF_V2_KEEP_OLD_EVALUATORS")
    (root / "loader" / "http.sqlite").rename(root / "loader" / "other")
    assert run(housekeeping.prune("periodic", B)) is None and present(root, A)


def test_requests_wait_while_versions_are_moved(root):
    async def scenario():
        await housekeeping.lock.acquire()
        waited = asyncio.create_task(housekeeping.wait_if_pruning())
        await asyncio.sleep(0.05)
        assert not waited.done()
        housekeeping.lock.release()
        await asyncio.wait_for(waited, 1)
    run(scenario())


def test_deleting_never_follows_symlinks_out(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep.txt").write_text("keep")
    tree = tmp_path / "tree"; (tree / "a").mkdir(parents=True)
    (tree / "a" / "link").symlink_to(outside)
    (tree / "a").chmod(0o555)
    housekeeping._remove(tree)
    assert not tree.exists() and (outside / "keep.txt").read_text() == "keep"


def test_disk_is_none_without_the_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CF_V2_ROOT", str(tmp_path / "missing"))
    assert housekeeping.disk() is None
