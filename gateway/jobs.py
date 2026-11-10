"""Evaluations nobody waits for any more are cancelled, so they never hold up the next one.

The evaluator runs one evaluation at a time and never stops one on its own. When a validator gives up on a design
(its deadline passed, the challenge server moved on, the validator restarted), the EDA server used to keep running
it, sometimes for a day: every design sent after it waited in the queue and timed out without ever starting. So
the gateway (rebuilt on every `make start`; the evaluator image is pinned) watches the evaluations:

- A validator (neuron 1.0.8+) sends its deadline with POST /v2/evaluate (headers X-CF-Request-Id and
  X-CF-Deadline, unix seconds). The gateway keeps it on disk (CF_V2_ROOT/gateway/jobs.json), so it holds across a
  restart of the validator, the gateway or the evaluator.
- Every CF_V2_WATCHDOG_SECONDS (15), and right after the gateway starts (so `make start` cleans up what an older
  version left behind), an unfinished evaluation is cancelled when
    1. its deadline passed (plus CF_V2_DEADLINE_GRACE_SECONDS, 30); or
    2. it has no deadline (sent by an older validator, or before this version) and is older than
       CF_V2_MAX_JOB_SECONDS (27600 = 7 h 40 min; the challenge server gives up after 7 h 10 min); or
    3. it has no deadline and was sent more than CF_V2_ABANDONED_GAP_SECONDS (900) before the newest unfinished
       one: an older validator works on one design at a time, so an older one is a design it gave up on. (An
       evaluation with a deadline is bounded by it alone: a batch's designs may be sent hours apart.)
  A validator's current evaluation matches none of them: nothing legitimate is cancelled.
- Cancelling uses the evaluator's own `cf-cancel` inside its container (through the Docker socket): the running
  stage and its sandboxes are stopped, the request ends ERROR / EVALUATION_CANCELLED (never scored) and the next
  evaluation starts.
- GET /v2/jobs lists the unfinished evaluations (phase, sent, started, deadline); POST /v2/cancel/<request_id>
  cancels one (validators call it when they give up); POST /v2/cancel-all cancels every one and cleans up the disk
  (the challenge server's kill switch, relayed by validators). When an evaluation started running is recorded, so a
  validator can tell a slow design from one that waited for the machine.

CF_V2_WATCHDOG=0 turns the automatic cancelling off (the endpoints stay).
"""
import asyncio
import json
import os
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")   # the loader's rule: never starts with "-"
router = APIRouter(prefix="/v2")
lock = asyncio.Lock()             # one watchdog pass or cancel at a time
recent: List[dict] = []           # the latest cancellations (GET /v2/health)
last_pass: Optional[str] = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def watchdog_seconds() -> float:
    return _env_float("CF_V2_WATCHDOG_SECONDS", 15)


def enabled() -> bool:
    return os.environ.get("CF_V2_WATCHDOG", "1") != "0"


def _root() -> Path:
    return Path(os.environ.get("CF_V2_ROOT") or "/var/lib/chipforge-v2")


def _iso(ts: Optional[float]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


# ------------------------------------------------------------------ what the gateway remembers (deadlines, starts)
class Store:
    """request_id -> {deadline, started_at, cancelled_at, cancel_reason, tries}; a JSON file next to the data."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.data: Dict[str, dict] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text()) or {}
            except (OSError, ValueError):
                self.data = {}

    def get(self, rid: str) -> dict:
        return self.data.setdefault(rid, {})

    def save(self, keep: Optional[set] = None) -> None:
        if keep is not None:                      # forget finished evaluations a day after they were last seen
            cutoff = time.time() - 86400
            self.data = {k: v for k, v in self.data.items() if k in keep or v.get("seen", 0) > cutoff}
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[watchdog] could not save {self.path}: {type(e).__name__}", flush=True)


_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(_root() / "gateway" / "jobs.json" if _root().is_dir() else None)
    return _store


def note_deadline(rid: Optional[str], deadline: Optional[str]) -> None:
    """From POST /v2/evaluate's X-CF-Request-Id / X-CF-Deadline headers (validators 1.0.8+). Called only once the
    evaluator accepted the request under that same ID (so no request can set another one's deadline). Never more
    than CF_V2_MAX_JOB_SECONDS ahead: no deadline lets an evaluation outlive the age cap."""
    if not rid or not ID.match(rid) or not deadline:
        return
    try:
        dl = float(deadline)
    except ValueError:
        return
    now = time.time()
    if not (now - 86400 < dl < now + 7 * 86400):
        return
    dl = min(dl, now + _env_float("CF_V2_MAX_JOB_SECONDS", 27600))
    rec = store().get(rid)
    rec["deadline"] = dl
    rec["seen"] = time.time()
    store().save()


# ------------------------------------------------------------------ the evaluator's queue and cancelling
class RealBackend:
    """The loader's own database (read only) and `cf-cancel` in the evaluator container."""

    def unfinished(self) -> List[dict]:
        db_path = _root() / "loader" / "http.sqlite"
        if not db_path.is_file():
            return []
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            rows = db.execute("SELECT request_id, phase, created FROM jobs WHERE phase NOT IN "
                              "('done','failed','cancelled','error') ORDER BY created").fetchall()
        finally:
            db.close()
        return [{"request_id": r[0], "phase": r[1], "created": float(r[2] or 0)} for r in rows]

    async def cancel(self, rid: str, reason: str) -> dict:
        return await docker_exec(["cf-cancel", "--reason", reason, "--", rid])


_backend = None


def backend():
    global _backend
    if _backend is None:
        import v2_mock
        _backend = v2_mock.MockBackend() if v2_mock.enabled() else RealBackend()
    return _backend


DOCKER_SOCK = "/var/run/docker.sock"


async def _docker(session: aiohttp.ClientSession, method: str, path: str, **kw):
    async with session.request(method, "http://docker" + path, **kw) as r:
        body = await r.read()
        return r.status, body


async def _evaluator_container(session) -> Optional[str]:
    """The v2-evaluator container of this compose project (CF_V2_EVALUATOR_CONTAINER overrides)."""
    name = os.environ.get("CF_V2_EVALUATOR_CONTAINER", "").strip()
    if name:
        return name
    project = None
    st, body = await _docker(session, "GET", f"/containers/{os.environ.get('HOSTNAME', '')}/json")
    if st == 200:
        project = (json.loads(body).get("Config", {}).get("Labels") or {}).get("com.docker.compose.project")
    labels = ["com.docker.compose.service=v2-evaluator"] + ([f"com.docker.compose.project={project}"] if project else [])
    st, body = await _docker(session, "GET", "/containers/json", params={"filters": json.dumps({"label": labels})})
    if st != 200:
        return None
    found = json.loads(body)
    return found[0]["Id"] if found else None


def _demux(raw: bytes) -> str:
    """Docker's multiplexed exec output (8-byte frame headers) -> text."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return (b"".join(out) if out else raw).decode(errors="replace")


async def docker_exec(cmd: List[str], timeout: float = 600) -> dict:
    """Run a command in the evaluator container. -> {exit, output}; exit None when Docker is unreachable."""
    if not os.path.exists(DOCKER_SOCK):
        return {"exit": None, "output": "the Docker socket is not mounted in the gateway"}
    conn = aiohttp.UnixConnector(path=DOCKER_SOCK)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        cid = await _evaluator_container(s)
        if not cid:
            return {"exit": None, "output": "the v2-evaluator container was not found"}
        st, body = await _docker(s, "POST", f"/containers/{cid}/exec",
                                 json={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True})
        if st != 201:
            return {"exit": None, "output": f"exec refused ({st}): {body[:200].decode(errors='replace')}"}
        eid = json.loads(body)["Id"]
        st, raw = await _docker(s, "POST", f"/exec/{eid}/start", json={"Detach": False, "Tty": False})
        st2, info = await _docker(s, "GET", f"/exec/{eid}/json")
        code = json.loads(info).get("ExitCode") if st2 == 200 else None
        return {"exit": code, "output": _demux(raw)[-2000:]}


# ------------------------------------------------------------------ the watchdog
def stale(jobs: List[dict], st: Store, now: float) -> Dict[str, str]:
    """request_id -> why it is cancelled (see the module docstring); the rest are left alone."""
    grace = _env_float("CF_V2_DEADLINE_GRACE_SECONDS", 30)
    cap = _env_float("CF_V2_MAX_JOB_SECONDS", 27600)
    gap = _env_float("CF_V2_ABANDONED_GAP_SECONDS", 900)
    newest = max((j["created"] for j in jobs), default=0)
    out = {}
    for j in jobs:
        rec = st.data.get(j["request_id"], {})
        if rec.get("deadline") and now > rec["deadline"] + grace:
            out[j["request_id"]] = f"its deadline passed {(now - rec['deadline']) / 60:.0f} min ago"
        elif not rec.get("deadline") and now - j["created"] > cap:
            out[j["request_id"]] = f"sent {(now - j['created']) / 3600:.1f} h ago, longer than any evaluation may run"
        elif not rec.get("deadline") and newest - j["created"] > gap:      # a deadline bounds the others (batches)
            out[j["request_id"]] = (f"a newer evaluation was sent {(newest - j['created']) / 60:.0f} min after it: the "
                                    "validator gave up on this one")
    return out


async def cancel(rid: str, reason: str, by: str) -> dict:
    """Cancel one evaluation (idempotent: a finished one is left as it is)."""
    rec = store().get(rid)
    rec["tries"] = rec.get("tries", 0) + 1
    rec["last_try"] = time.time()
    result = await backend().cancel(rid, f"{reason} ({by})"[:200])
    ok = result.get("exit") in (0, 3)            # 0 cancelled or already finished, 3 unknown here
    if ok:
        rec["cancelled_at"], rec["cancel_reason"] = time.time(), reason
        recent.insert(0, {"request_id": rid, "at": _iso(time.time()), "reason": reason, "by": by})
        del recent[20:]
    store().save()
    print(f"[watchdog] {'cancelled' if ok else 'could not cancel'} {rid} ({by}): {reason}"
          + ("" if ok else f" -> {result.get('output', '')[-300:]}"), flush=True)
    return {"request_id": rid, "cancelled": ok, "exit": result.get("exit"), "output": result.get("output", "")[-600:]}


async def watch_once(now_fn: Callable[[], float] = time.time) -> List[str]:
    """One pass: note when evaluations started, cancel the abandoned ones. -> request IDs cancelled."""
    global last_pass
    async with lock:
        jobs = await asyncio.to_thread(backend().unfinished)
        st, now = store(), now_fn()
        for j in jobs:
            rec = st.get(j["request_id"])
            rec["seen"] = now
            if j["phase"] == "running" and not rec.get("started_at"):
                rec["started_at"] = now
        st.save(keep={j["request_id"] for j in jobs})
        last_pass = _iso(now)
        if not enabled():
            return []
        done = []
        for rid, why in stale(jobs, st, now).items():
            rec = st.get(rid)
            if rec.get("last_try") and now - rec["last_try"] < 120:      # a failed cancel is retried every 2 min
                continue
            if (await cancel(rid, why, "watchdog"))["cancelled"]:
                done.append(rid)
        return done


async def loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await watch_once()
        except Exception as e:                       # never let the watchdog take the gateway down
            print(f"[watchdog] pass failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        await asyncio.sleep(watchdog_seconds())


def view(j: dict, rec: dict) -> dict:
    return {"request_id": j["request_id"], "phase": j["phase"], "sent_at": _iso(j["created"]),
            "started_at": _iso(rec.get("started_at")), "deadline": _iso(rec.get("deadline")),
            "sent_unix": j["created"], "started_unix": rec.get("started_at"), "deadline_unix": rec.get("deadline")}


def health() -> dict:
    return {"enabled": enabled(), "last_pass": last_pass, "recent_cancellations": recent[:5]}


# ------------------------------------------------------------------ routes (same in mock mode)
def _refused(request: Request):
    """With V2_ADMIN_TOKEN set (as for installs), listing and cancelling need `Authorization: Bearer <token>`: the
    list names the designs being evaluated, and a cancel stops one. Validators send EDA_ADMIN_TOKEN."""
    import secrets
    tok = os.environ.get("V2_ADMIN_TOKEN", "")
    if tok and not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + tok):
        return JSONResponse({"error": "ADMIN_TOKEN_REQUIRED", "message": "This needs the admin token."}, status_code=401)
    return None


@router.get("/jobs")
async def list_jobs(request: Request):
    refused = _refused(request)
    if refused:
        return refused
    jobs = await asyncio.to_thread(backend().unfinished)
    st = store()
    return {"jobs": [view(j, st.data.get(j["request_id"], {})) for j in jobs], "watchdog": health()}


@router.post("/cancel/{request_id}")
async def cancel_route(request_id: str, request: Request):
    refused = _refused(request)
    if refused:
        return refused
    if not ID.match(request_id):
        return JSONResponse({"error": "NOT_FOUND", "message": "Unknown request id."}, status_code=404)
    reason = (request.headers.get("X-CF-Reason") or "the validator gave up on it")[:150]
    async with lock:
        out = await cancel(request_id, reason, "validator")
    return JSONResponse(out, status_code=200 if out["cancelled"] else 502)


@router.post("/cancel-all")
async def cancel_all(request: Request):
    """Kill switch (the challenge server's KILL_EVALUATIONS, relayed by validators 1.0.8+): cancel every unfinished
    evaluation now, then clean up the disk (old evaluator versions, housekeeping.py)."""
    refused = _refused(request)
    if refused:
        return refused
    reason = (request.headers.get("X-CF-Reason") or "kill switch: every evaluation stopped")[:150]
    async with lock:
        unfinished = await asyncio.to_thread(backend().unfinished)
        results = [await cancel(j["request_id"], reason, "kill switch") for j in unfinished]
    cleanup = None
    try:
        import housekeeping
        from v2_proxy import active_evaluator
        cleanup = await housekeeping.prune("kill switch", await active_evaluator())
    except Exception as e:                           # cleaning up is best effort; the cancels are what matter
        print(f"[watchdog] cleanup after the kill switch failed: {type(e).__name__}", flush=True)
    print(f"[watchdog] kill switch: {sum(r['cancelled'] for r in results)} of {len(results)} evaluation(s) "
          f"cancelled ({reason})", flush=True)
    return JSONResponse({"cancelled": [r["request_id"] for r in results if r["cancelled"]],
                         "failed": [r["request_id"] for r in results if not r["cancelled"]], "cleanup": cleanup},
                        status_code=200 if all(r["cancelled"] for r in results) else 502)
