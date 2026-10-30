"""v2 mock mode (gateway/v2_mock.py): real v2 request/response shapes, chain_commit checks, deterministic
results across servers, per-design MOCK.json overrides."""
import hashlib
import importlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _zip(extra=None, tag="a"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("rtl/top.sv", f"module top(); endmodule // {tag}\n")
        if extra is not None:
            z.writestr("MOCK.json", json.dumps(extra))
    return buf.getvalue()


def _commit(data, **over):
    c = {"schema": "cf-chain-commit/1", "subtensor_genesis_hash": "ab" * 32, "netuid": 108, "hotkey": "5H",
         "block_number": 1, "block_hash": "cd" * 32, "block_timestamp_ms": 1, "package_sha256": hashlib.sha256(data).hexdigest()}
    c.update(over)
    return json.dumps(c)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "mock.json"
    cfg.write_text(json.dumps({"delay_seconds": 0, "status": "PASSED", "S": [1.0, 6.0]}))
    monkeypatch.setenv("EDA_MOCK_FILE", str(cfg))
    for m in ("v2_mock", "main"):
        sys.modules.pop(m, None)
    main = importlib.import_module("main")
    importlib.import_module("v2_mock")._jobs.clear()
    c = TestClient(main.app)
    c.cfg = cfg
    return c


def evaluate(c, data, rid="r1", mode="T2", commit=True, **fields):
    form = {"mode": mode, "request_id": rid, "submitter": "5Miner", **fields}
    if commit:
        form["chain_commit"] = commit if isinstance(commit, str) else _commit(data)
    return c.post("/v2/evaluate", data=form, files={"design_zip": ("d.zip", data, "application/zip")})


def test_health_names_the_active_evaluator(client):
    h = client.get("/v2/health").json()
    assert h["ready"] and h["mock"] and len(h["active_evaluator"]) == 64


def test_accept_then_result_like_the_real_server(client):
    client.cfg.write_text(json.dumps({"delay_seconds": 1, "S": 4.5}))
    data = _zip()
    sha = client.get("/v2/health").json()["active_evaluator"]
    r = evaluate(client, data, evaluator_sha256=sha)
    assert r.status_code == 202 and r.json()["phase"] == "running" and r.json()["status_url"] == "/v2/status/r1"
    assert client.get("/v2/status/r1").status_code == 202
    time.sleep(1.1)
    res = client.get("/v2/status/r1")
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "cf-response/1" and body["status"] == "PASSED" and body["tier_reached"] == "T2"
    assert body["public_report"]["score"]["S"]["value"] == 4.5 and body["public_report"]["verdict"]["score_S"] == 4.5
    assert len(body["projection_sha256"]) == 64 and body["mock"] is True


def test_chain_commit_rules(client):
    data = _zip()
    assert evaluate(client, data, commit=False).json()["error"]["code"] == "CHAIN_COMMIT_REQUIRED"
    bad = _commit(data, package_sha256="00" * 32)
    assert evaluate(client, data, commit=bad).json()["error"]["code"] == "INVALID_CHAIN_COMMIT"
    assert evaluate(client, data, rid="dev", mode="DEV", commit=False).status_code == 202


def test_wrong_evaluator_and_request_id_conflict(client):
    data = _zip()
    assert evaluate(client, data, evaluator_sha256="ff" * 32).status_code == 404
    assert evaluate(client, data).status_code == 202
    assert evaluate(client, data).status_code in (200, 202)                 # same request: same job
    assert evaluate(client, _zip(tag="other")).status_code == 409


def test_same_design_same_result_and_hash_on_two_servers(client):
    data = _zip()
    a = evaluate(client, data, rid="v1").json()
    a = client.get("/v2/status/v1").json()
    b = evaluate(client, data, rid="v2").json()
    b = client.get("/v2/status/v2").json()
    assert a["projection_sha256"] == b["projection_sha256"]
    assert a["public_report"]["score"]["S"]["value"] == b["public_report"]["score"]["S"]["value"]


def test_s_offset_simulates_a_dishonest_validator(client):
    data = _zip()
    evaluate(client, data, rid="honest")
    honest = client.get("/v2/status/honest").json()
    client.cfg.write_text(json.dumps({"delay_seconds": 0, "S": [1.0, 6.0], "S_offset": 3}))
    evaluate(client, data, rid="cheat")
    cheat = client.get("/v2/status/cheat").json()
    assert cheat["public_report"]["score"]["S"]["value"] == pytest.approx(honest["public_report"]["score"]["S"]["value"] + 3)
    assert cheat["projection_sha256"] != honest["projection_sha256"]


def test_design_mock_json_overrides(client):
    evaluate(client, _zip({"status": "REJECTED", "failure_code": "NPU_PORT_MISMATCH"}), rid="rej")
    r = client.get("/v2/status/rej").json()
    assert r["status"] == "REJECTED" and r["public_report"]["verdict"]["failure_code"] == "NPU_PORT_MISMATCH"
    evaluate(client, _zip({"S": 7.25}, tag="b"), rid="s")
    assert client.get("/v2/status/s").json()["public_report"]["score"]["S"]["value"] == 7.25
    evaluate(client, _zip({"status": "INCOMPLETE"}, tag="c"), rid="inc")
    assert client.get("/v2/status/inc").json()["status"] == "INCOMPLETE"


def test_unknown_status_404(client):
    assert client.get("/v2/status/nope").status_code == 404
