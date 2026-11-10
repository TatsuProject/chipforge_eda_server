"""Gateway housekeeping: EDA version, free disk, deleting old evaluator versions. Run: pytest gateway/tests"""
import asyncio, os, sqlite3, sys, time
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


# ------------------------------------------------------------------ finished designs' files (sweep_designs)
def design_tree(root, sha, sub, old=True):
    paths = [root / "state" / sha / "components" / "campaign" / "wp-o" / sub,
             root / "state" / sha / "components" / "physical" / "wp-o" / sub,
             root / "state" / sha / "runs" / "jobs" / sub]
    for p in paths:
        (p / "slot1").mkdir(parents=True, exist_ok=True)
        (p / "slot1" / "big.bin").write_bytes(b"x" * 1000)
        if old:
            t = time.time() - 3600
            for q in (p, p / "slot1", p / "slot1" / "big.bin"):
                os.utime(q, (t, t))
    return paths


def evaluator_db(root, sha, rows):
    db = sqlite3.connect(root / "state" / sha / "http.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT, job_id TEXT)")
    db.executemany("INSERT INTO requests VALUES (?,?)", rows); db.commit(); db.close()


SUBS = {k: "sub_" + c * 24 for k, c in (("done", "a"), ("running", "b"), ("timedout", "c"), ("fresh", "d"), ("queued", "e"))}


@pytest.fixture
def designs(root):
    import time as _t
    version(root, A)
    sql(root, "ALTER TABLE jobs ADD COLUMN created REAL")
    for rid, phase in (("r-done", "done"), ("r-running", "running"), ("r-timedout", "done"), ("r-fresh", "done"),
                       ("r-queued", "queued")):
        sql(root, "INSERT INTO jobs (request_id, sha, phase) VALUES (?,?,?)", rid, A, phase)
    evaluator_db(root, A, [("r-" + k, v) for k, v in SUBS.items()])
    trees = {k: design_tree(root, A, v, old=(k != "fresh")) for k, v in SUBS.items()}
    shared = [root / "state" / A / "components" / "physical" / "cache" / "x", root / "cache" / "platform-sim" / "y"]
    for p in shared:
        p.mkdir(parents=True); (p / "f").write_bytes(b"keep")
    return trees, shared


def test_finished_designs_files_are_deleted_and_the_running_ones_kept(root, designs):
    trees, shared = designs
    out = asyncio.run(housekeeping.sweep_designs("test"))
    gone = lambda k: not any(p.exists() for p in trees[k])
    kept = lambda k: all(p.exists() for p in trees[k])
    assert gone("done") and gone("timedout")                       # passed / failed / timed out / cancelled
    assert kept("running") and kept("queued")                     # unfinished: never touched
    assert kept("fresh")                                          # finished seconds ago: after the quiet time
    assert all((p / "f").exists() for p in shared)                # caches every evaluation uses stay
    assert sorted(out["designs"]) == [SUBS["done"], SUBS["timedout"]] and out["folders"] == 6
    assert housekeeping.decorate({})["last_design_cleanup"]["designs"] == out["designs"]
    assert not list((root / "tmp" / "cf-old-evaluators").iterdir())   # emptied


def test_simulation_scratch_only_while_nothing_runs(root, designs):
    scratch = root / "tmp" / "cf-sim-abc"
    scratch.mkdir(parents=True); (scratch / "w").write_bytes(b"x")
    t = time.time() - 3600; os.utime(scratch, (t, t)); os.utime(scratch / "w", (t, t))
    asyncio.run(housekeeping.sweep_designs("test"))
    assert scratch.exists()                                       # an evaluation is running: it may be its scratch
    sql(root, "UPDATE jobs SET phase='done'")
    asyncio.run(housekeeping.sweep_designs("test"))
    assert not scratch.exists()


def test_nothing_is_deleted_when_the_databases_cannot_be_read(root, designs):
    trees, _ = designs
    (root / "loader" / "http.sqlite").write_bytes(b"not a database")
    assert asyncio.run(housekeeping.sweep_designs("test")) is None
    assert all(p.exists() for ps in trees.values() for p in ps)


def test_switched_off(root, designs, monkeypatch):
    monkeypatch.setenv("CF_V2_KEEP_DESIGN_FILES", "1")
    trees, _ = designs
    assert asyncio.run(housekeeping.sweep_designs("test")) is None and all(p.exists() for p in trees["done"])


def test_a_symlink_named_like_a_design_is_not_followed(root, designs, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "precious").write_bytes(b"keep")
    link = root / "state" / A / "runs" / "jobs" / ("sub_" + "f" * 24)
    link.symlink_to(outside)
    asyncio.run(housekeeping.sweep_designs("test"))
    assert (outside / "precious").exists()
