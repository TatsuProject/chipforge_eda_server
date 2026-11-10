"""Abandoned evaluations are cancelled (gateway/jobs.py). Run: pytest gateway/tests"""
import asyncio
import importlib
import io
import json
import os
import sqlite3
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jobs  # noqa: E402

H = 3600


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    (r / "loader").mkdir(parents=True)
    db = sqlite3.connect(r / "loader" / "http.sqlite")
    db.executescript("CREATE TABLE jobs (request_id TEXT, sha TEXT, phase TEXT, created REAL, forwarded INTEGER);"
                     "CREATE TABLE installs (sha TEXT, attempt INTEGER, phase TEXT, error TEXT, unsigned INTEGER);")
    db.commit(); db.close()
    monkeypatch.setenv("CF_V2_ROOT", str(r))
    for k in ("CF_V2_WATCHDOG", "CF_V2_MAX_JOB_SECONDS", "CF_V2_ABANDONED_GAP_SECONDS", "CF_V2_DEADLINE_GRACE_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(jobs, "_store", None)
    monkeypatch.setattr(jobs, "_backend", jobs.RealBackend())
    jobs.recent.clear()
    return r


def job(root, rid, phase, created):
    db = sqlite3.connect(root / "loader" / "http.sqlite")
    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (rid, "a" * 64, phase, created, int(phase == "running")))
    db.commit(); db.close()


@pytest.fixture
def docker(monkeypatch):
    """cf-cancel as the evaluator would answer it."""
    calls = []

    async def fake_exec(cmd, timeout=600):
        calls.append(cmd)
        return {"exit": 0, "output": json.dumps({"request_id": cmd[1], "cancelled": True})}
    monkeypatch.setattr(jobs, "docker_exec", fake_exec)
    return calls


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_the_stuck_validators_are_cleaned_up(root, docker):
    """Ours on Nov 10: one evaluation running for 25 h and three designs queued behind it, the newest the current one."""
    now = time.time()
    job(root, "old-running", "running", now - 25 * H)
    job(root, "abandoned-1", "queued", now - 17.5 * H)
    job(root, "abandoned-2", "queued", now - 10.5 * H)
    job(root, "current", "queued", now - 3.3 * H)
    done = run(jobs.watch_once())
    assert sorted(done) == ["abandoned-1", "abandoned-2", "old-running"]
    assert [c[1] for c in docker] == sorted(done, key=lambda r: ["old-running", "abandoned-1", "abandoned-2"].index(r))
    assert all(c[0] == "cf-cancel" and c[2] == "--reason" for c in docker)


def test_a_healthy_validator_loses_nothing(root, docker):
    """RT21: only its current evaluation is unfinished (running for 3 h, no deadline yet): left alone."""
    now = time.time()
    job(root, "current", "running", now - 3 * H)
    assert run(jobs.watch_once()) == [] and docker == []
    assert jobs.store().data["current"]["started_at"]                          # its start is recorded


def test_a_batch_sent_together_is_kept(root, docker):
    now = time.time()
    for i in range(4):
        job(root, f"b{i}", "queued" if i else "running", now - 2 * H + i * 20)
    assert run(jobs.watch_once()) == [] and docker == []


def test_a_passed_deadline_is_enforced(root, docker, monkeypatch):
    now = time.time()
    job(root, "late", "running", now - 2 * H)
    jobs.note_deadline("late", str(now - 60))                                  # passed a minute ago
    job(root, "fine", "running", now - 2 * H + 5)
    jobs.note_deadline("fine", str(now + H))
    assert run(jobs.watch_once()) == ["late"]
    monkeypatch.setattr(jobs, "_store", None)                                   # the gateway restarted
    assert jobs.store().data["fine"]["deadline"] == pytest.approx(now + H)       # deadlines are kept on disk


def test_a_deadline_overrides_the_age_cap(root, docker):
    """A validator that sent a deadline later than the cap is trusted with it."""
    now = time.time()
    job(root, "long", "running", now - 8 * H)
    jobs.note_deadline("long", str(now + 600))
    assert run(jobs.watch_once()) == []


def test_switched_off(root, docker, monkeypatch):
    monkeypatch.setenv("CF_V2_WATCHDOG", "0")
    job(root, "old", "running", time.time() - 25 * H)
    assert run(jobs.watch_once()) == [] and docker == []


def test_a_failed_cancel_is_retried_later(root, monkeypatch):
    calls = []

    async def failing(cmd, timeout=600):
        calls.append(cmd)
        return {"exit": None, "output": "the v2-evaluator container was not found"}
    monkeypatch.setattr(jobs, "docker_exec", failing)
    now = time.time()
    job(root, "old", "running", now - 25 * H)
    assert run(jobs.watch_once()) == [] and len(calls) == 1
    run(jobs.watch_once())
    assert len(calls) == 1                                                      # not hammered
    assert run(jobs.watch_once(lambda: now + 200)) == [] and len(calls) == 2    # retried after 2 min


def test_bad_deadline_headers_are_ignored(root):
    for rid, dl in (("x", "soon"), ("../etc", "1"), ("y", str(time.time() + 30 * 86400)), (None, "1"), ("z", None)):
        jobs.note_deadline(rid, dl)
    assert jobs.store().data == {}


# ------------------------------------------------------------------ Docker: the real exec protocol, on a fake socket
def test_cf_cancel_runs_in_the_evaluator_container(tmp_path, monkeypatch):
    from aiohttp import web
    sock = str(tmp_path / "docker.sock")
    seen = {}

    async def own(request):                                    # the gateway's own container -> compose project
        return web.json_response({"Config": {"Labels": {"com.docker.compose.project": "chipforge_eda_server"}}})

    async def containers(request):
        seen["filters"] = json.loads(request.query["filters"])
        return web.json_response([{"Id": "evaluator123"}])

    async def create(request):
        seen["container"] = request.match_info["cid"]
        seen["cmd"] = (await request.json())["Cmd"]
        return web.json_response({"Id": "exec9"}, status=201)

    async def start(request):
        out = json.dumps({"cancelled": True}).encode()
        return web.Response(body=struct.pack(">BxxxI", 1, len(out)) + out)

    async def inspect(request):
        return web.json_response({"ExitCode": 0})

    async def scenario():
        app = web.Application()
        app.router.add_get("/containers/gw/json", own)
        app.router.add_get("/containers/json", containers)
        app.router.add_post("/containers/{cid}/exec", create)
        app.router.add_post("/exec/exec9/start", start)
        app.router.add_get("/exec/exec9/json", inspect)
        runner = web.AppRunner(app); await runner.setup()
        site = web.UnixSite(runner, sock); await site.start()
        try:
            return await jobs.docker_exec(["cf-cancel", "rid1", "--reason", "test"])
        finally:
            await runner.cleanup()
    monkeypatch.setattr(jobs, "DOCKER_SOCK", sock)
    monkeypatch.setenv("HOSTNAME", "gw")
    monkeypatch.delenv("CF_V2_EVALUATOR_CONTAINER", raising=False)
    out = run(scenario())
    assert out == {"exit": 0, "output": '{"cancelled": true}'}
    assert seen["container"] == "evaluator123" and seen["cmd"] == ["cf-cancel", "rid1", "--reason", "test"]
    assert set(seen["filters"]["label"]) == {"com.docker.compose.service=v2-evaluator",
                                             "com.docker.compose.project=chipforge_eda_server"}


def test_no_docker_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "DOCKER_SOCK", str(tmp_path / "none.sock"))
    assert run(jobs.docker_exec(["cf-cancel", "x"]))["exit"] is None


# ------------------------------------------------------------------ the mock: one evaluation at a time, cancellable
def _zip(tag):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("top.v", f"// {tag}\n")
    return buf.getvalue()


@pytest.fixture
def mock(tmp_path, monkeypatch):
    cfg = tmp_path / "eda_mock.json"
    cfg.write_text(json.dumps({"delay_seconds": 2, "status": "PASSED"}))
    monkeypatch.setenv("EDA_MOCK_FILE", str(cfg))
    monkeypatch.setenv("CF_V2_ROOT", str(tmp_path / "nothing"))
    monkeypatch.setenv("CF_V2_WATCHDOG", "0")                 # the test drives it
    import v2_mock, main
    importlib.reload(v2_mock); importlib.reload(jobs); importlib.reload(main)
    yield TestClient(main.app)
    monkeypatch.delenv("EDA_MOCK_FILE")                      # leave the modules as the other tests expect them
    importlib.reload(v2_mock); importlib.reload(jobs); importlib.reload(main)


def send(c, rid, tag, deadline=None):
    h = {"X-CF-Request-Id": rid, **({"X-CF-Deadline": str(deadline)} if deadline else {})}
    return c.post("/v2/evaluate", files={"design_zip": ("d.zip", _zip(tag), "application/zip")},
                  data={"mode": "DEV", "request_id": rid}, headers=h)


def test_mock_runs_one_at_a_time_and_a_cancel_frees_the_machine(mock):
    assert send(mock, "a", "a").status_code == 202
    assert send(mock, "b", "b").status_code == 202
    j = {x["request_id"]: x["phase"] for x in mock.get("/v2/jobs").json()["jobs"]}
    assert j == {"a": "running", "b": "queued"}
    assert mock.get("/v2/status/b").headers["X-CF-Phase"] == "queued"
    r = mock.post("/v2/cancel/a", headers={"X-CF-Reason": "deadline passed"})
    assert r.status_code == 200 and r.json()["cancelled"]
    done = mock.get("/v2/status/a")
    assert done.status_code == 200 and done.json()["status"] == "ERROR"
    assert done.json()["problems"][0]["code"] == "EVALUATION_CANCELLED"
    assert {x["request_id"]: x["phase"] for x in mock.get("/v2/jobs").json()["jobs"]} == {"b": "running"}
    time.sleep(2.2)
    assert mock.get("/v2/status/b").json()["status"] == "PASSED"


def test_mock_watchdog_cancels_past_its_deadline(mock, monkeypatch):
    monkeypatch.setenv("CF_V2_WATCHDOG", "1")
    send(mock, "late", "x", deadline=time.time() - 120)
    send(mock, "next", "y", deadline=time.time() + 600)
    assert run(jobs.watch_once()) == ["late"]
    assert mock.get("/v2/status/late").json()["problems"][0]["code"] == "EVALUATION_CANCELLED"
    assert mock.get("/v2/jobs").json()["jobs"][0]["request_id"] == "next"
    assert mock.get("/v2/health").json()["watchdog"]["recent_cancellations"][0]["request_id"] == "late"


def test_unknown_or_bad_ids(mock):
    assert mock.post("/v2/cancel/..%2F..").status_code in (404, 405)
    r = mock.post("/v2/cancel/nope")
    assert r.status_code == 200 and r.json()["cancelled"]                       # unknown: nothing to stop
