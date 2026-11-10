"""EDA server housekeeping, done by the gateway (rebuilt on every `make start`; the evaluator image is pinned).

- The EDA server's own version (VERSION next to this file, bumped with every change to main): eda_version in
  GET /health and GET /v2/health, so validators report it and the website shows it.
- Free disk where evaluations run (the data root, CF_V2_ROOT): disk in GET /v2/health.
- Old evaluator versions are deleted. Each version keeps state/<sha> (its runs and caches, tens of GB),
  escrow/<sha> and evaluators/<sha>, and the evaluator never removes them: after a few bundle updates the disk
  fills and the next install fails its preflight (100 GiB free needed). Deleted: every version nothing needs.
  Kept: the newest ready one, the one the evaluator service runs, and any a pending request or an install in
  progress needs (read from the loader's database). archives/<sha>.zip and .sig stay, so a version asked for
  again is reinstalled from them. Runs before an evaluator install is forwarded (it frees the space the
  preflight checks) and every CF_V2_PRUNE_SECONDS (600). While it decides and moves the trees aside (renames,
  instant) new /v2 evaluations and installs wait; the trees are then deleted in the background.
  last_cleanup in GET /v2/health. CF_V2_KEEP_OLD_EVALUATORS=1 turns it off.
"""
import asyncio
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{64}\Z")
TREES = ("state", "escrow", "evaluators")
_VERSION_FILE = Path(__file__).with_name("VERSION")
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.is_file() else "unknown"

lock = asyncio.Lock()          # held while deciding and moving: /v2/evaluate and installs wait for it
last_cleanup = None            # the latest cleanup that removed something


def root() -> Path:
    return Path(os.environ.get("CF_V2_ROOT") or "/var/lib/chipforge-v2")


def prune_seconds() -> float:
    return float(os.environ.get("CF_V2_PRUNE_SECONDS") or 600)


def enabled() -> bool:
    return os.environ.get("CF_V2_KEEP_OLD_EVALUATORS") != "1" and (root() / "loader" / "http.sqlite").is_file()


def _versions_on_disk() -> set:
    r = root()
    return {p.name for d in TREES if (r / d).is_dir() for p in (r / d).iterdir() if SHA.fullmatch(p.name)}


def disk():
    """Free and total disk of the data root, and how many evaluator versions have data on it (None: not mounted)."""
    try:
        if not root().is_dir():
            return None
        usage = shutil.disk_usage(root())
        return {"free_gib": round(usage.free / 2**30, 1), "total_gib": round(usage.total / 2**30, 1),
                "evaluator_versions": len(_versions_on_disk())}
    except OSError:
        return None


def decorate(health: dict) -> dict:
    """What the gateway adds to the evaluator's /v2/health."""
    import jobs
    return {**health, "eda_version": VERSION, "disk": disk(), "last_cleanup": last_cleanup, "watchdog": jobs.health()}


def in_use(active):
    """Versions that must stay (see the module docstring), from the loader's database."""
    keep = {active} if active else set()
    db = sqlite3.connect(root() / "loader" / "http.sqlite", timeout=10)
    try:
        row = db.execute("SELECT sha FROM installs WHERE phase='ready' ORDER BY rowid DESC LIMIT 1").fetchone()
        if row:
            keep.add(row[0])
        keep |= {r[0] for r in db.execute("SELECT DISTINCT sha FROM jobs WHERE phase!='done'")}
        keep |= {r[0] for r in db.execute(
            "SELECT sha FROM installs i WHERE attempt=(SELECT MAX(attempt) FROM installs WHERE sha=i.sha) "
            "AND phase NOT IN ('ready','failed')")}
    finally:
        db.close()
    return keep


def _trash() -> Path:
    t = root() / "tmp" / "cf-old-evaluators"
    t.mkdir(parents=True, exist_ok=True)
    return t


def _move(victims):
    """Move the victims' trees aside (renames on the same disk: instant). Returns the versions moved."""
    trash, moved = _trash(), []
    for sha in victims:
        for d in TREES:
            p = root() / d / sha
            if p.exists() or p.is_symlink():
                if p.is_dir() and not p.is_symlink():
                    os.chmod(p, 0o755)                   # a read-only directory cannot move to another parent
                os.rename(p, trash / f"{d}-{sha}-{time.time_ns()}")
        moved.append(sha)
    return moved


def _remove(tree: Path):
    """Delete a tree the evaluator may have made read-only. Never follows symlinks out of it."""
    try:
        if tree.is_symlink() or tree.is_file():
            tree.unlink()
            return
        if not tree.is_dir():
            return
        os.chmod(tree, 0o755)
        for dirpath, dirnames, _ in os.walk(tree, followlinks=False):
            for d in dirnames:
                path = os.path.join(dirpath, d)
                if not os.path.islink(path):
                    try:
                        os.chmod(path, 0o755)
                    except OSError:
                        pass
    except OSError:
        pass
    shutil.rmtree(tree, ignore_errors=True)


def _empty_trash():
    for p in list(_trash().iterdir()):
        _remove(p)


async def prune(reason: str, active_evaluator):
    """Delete old evaluator versions. active_evaluator: what the evaluator service runs (from its /health);
    pass False when it is unknown (unreachable): nothing is deleted then."""
    global last_cleanup
    if not enabled() or active_evaluator is False:
        return None
    try:
        before = shutil.disk_usage(root()).free
        async with lock:
            keep = await asyncio.to_thread(in_use, active_evaluator)
            if not keep:
                return None                                  # nothing installed or needed yet
            victims = sorted(_versions_on_disk() - keep)
            if not victims:
                return None
            moved = await asyncio.to_thread(_move, victims)
        await asyncio.to_thread(_empty_trash)
        usage = shutil.disk_usage(root())
    except (OSError, sqlite3.Error) as e:
        print(f"[cleanup] skipped ({reason}): {type(e).__name__}", flush=True)
        return None
    last_cleanup = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "reason": reason,
                    "evaluators": moved, "freed_gib": round(max(0, usage.free - before) / 2**30, 1),
                    "free_gib": round(usage.free / 2**30, 1)}
    print(f"[cleanup] {reason}: deleted {len(moved)} old evaluator version(s) {[s[:12] for s in moved]}, "
          f"freed {last_cleanup['freed_gib']} GiB, {last_cleanup['free_gib']} GiB free", flush=True)
    return last_cleanup


async def wait_if_pruning():
    """New evaluations and installs wait while old versions are being moved aside (a few seconds at most)."""
    async with lock:
        pass
