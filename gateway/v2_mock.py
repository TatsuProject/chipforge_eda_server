"""v2 mock mode (TESTING ONLY): answer /v2/health, /v2/evaluate and /v2/status like the real v2 evaluator,
without running anything, so validators, the challenge server and the website can be tested end to end
in minutes instead of hours.

Enabled by EDA_MOCK_FILE (a JSON file, re-read on every /v2/evaluate). See gateway/mock_result.example.json
and the README's "Mock mode". Never enable it on a validator that evaluates real submissions.

What it checks like the real evaluator: the multipart fields, the request ID, T1/T2 need a chain_commit
(cf-chain-commit/1, 8 fields, its package_sha256 must be the sha256 of the uploaded zip), the evaluator
sha256 must be the active one, and a reused request ID must name the same bytes / chain_commit.

What it returns: 202 while "running" (delay_seconds), then a cf-response/1 result with status, tier, the
score S in public_report (built from a sample report), and projection_sha256. The projection hash is a
function of the design, its chain_commit and the result, so two honest mock validators report the same
hash for the same submission, and a validator with a different S (S_offset) reports a different one.

Per-design control: a design zip may contain MOCK.json with any of the result fields below; those win over
EDA_MOCK_FILE for that design (e.g. one miner's design REJECTED, another slow, another S 7.5).

Result fields:
  delay_seconds   seconds until the result is ready (default 10)
  status          PASSED | REJECTED | INCOMPLETE | ERROR (default PASSED)
  S               a number, or [low, high]: a value derived from the design's sha256, so every validator
                  gets the same S for the same design (default [1.0, 6.0])
  S_offset        added to S by THIS server only: simulates a dishonest or misconfigured validator
  failure_code    for REJECTED (default NPU_PORT_MISMATCH)
  projection      "deterministic" (default) or "random" (simulates a non-deterministic evaluator)
"""
import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v2")

EDA_MOCK_FILE = os.environ.get("EDA_MOCK_FILE", "").strip()
MOCK_EVALUATOR_SHA = os.environ.get("EDA_MOCK_EVALUATOR_SHA256", "").strip() or ("0" * 63 + "1")
REPORTS = Path(__file__).resolve().parent / "mock_reports"
ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CHAIN_FIELDS = {"schema", "subtensor_genesis_hash", "netuid", "hotkey", "block_number", "block_hash",
                "block_timestamp_ms", "package_sha256"}
MODES = ("T0", "T1", "T2", "DEV")
STATUSES = ("PASSED", "REJECTED", "INCOMPLETE", "ERROR")
_jobs: dict = {}


def enabled() -> bool:
    return bool(EDA_MOCK_FILE)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _config() -> dict:
    try:
        cfg = json.loads(Path(EDA_MOCK_FILE).read_text())
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _design_overrides(zip_bytes: bytes) -> dict:
    """MOCK.json at the top level of the design zip (testing only)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "MOCK.json"]
            if names:
                cfg = json.loads(z.read(names[0]).decode())
                return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return {}


def _score(spec, digest: str) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, list) and len(spec) == 2:
        lo, hi = float(spec[0]), float(spec[1])
        frac = int(digest[:12], 16) / float(16 ** 12)          # same design -> same value on every server
        return round(lo + (hi - lo) * frac, 6)
    return 3.0


def _check_chain_commit(raw: str, digest: str) -> dict:
    try:
        cc = json.loads(raw)
    except ValueError:
        raise ValueError("chain_commit is not JSON")
    if not isinstance(cc, dict) or set(cc) != CHAIN_FIELDS or cc.get("schema") != "cf-chain-commit/1":
        raise ValueError("chain_commit must be a cf-chain-commit/1 object with exactly its 8 fields")
    for key in ("subtensor_genesis_hash", "block_hash", "package_sha256"):
        if not isinstance(cc[key], str) or not HEX64.match(cc[key]):
            raise ValueError(f"{key} must be 64 lowercase hex characters")
    if cc["package_sha256"] != digest:
        raise ValueError("package_sha256 does not match the sha256 of design_zip")
    return cc


def _report(status: str, s: float, cfg: dict, tier: str) -> dict:
    name = {"PASSED": "passed", "REJECTED": "rejected"}.get(status, "incomplete")
    try:
        report = json.loads((REPORTS / f"{name}.json").read_text())
    except Exception:
        report = {"schema": "cf-public-report/1", "verdict": {}, "score": {}, "outcome": {}}
    report["sample"], report["sample_note"] = False, "MOCK: generated by the EDA gateway's mock mode; no EDA tools ran"
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    verdict = report.setdefault("verdict", {})
    verdict["status"], verdict["tier_reached"] = status, tier
    if status == "PASSED":
        verdict.update(state="SCORED", score_S=s, score_P=s, failure_code=None, fault=None)
        score = report.setdefault("score", {})
        if isinstance(score.get("S"), dict):
            score["S"]["value"] = s
            score["S"]["lo"] = score["S"]["hi"] = s
        if isinstance(score.get("P"), dict):           # the sample's area term R is 1: P = S keeps P x R = S
            score["P"]["value"] = s
            score["P"].pop("lo", None), score["P"].pop("hi", None)
        else:
            score["S"] = {"value": s, "unit": "ratio", "label": "PROJECTED", "source": "mock"}
    elif status == "REJECTED":
        verdict.update(state="REJECTED", score_S=None, score_P=None,
                       failure_code=cfg.get("failure_code", "NPU_PORT_MISMATCH"), fault="miner")
    else:
        verdict.update(state=status, score_S=None, score_P=None, fault="system", retryable=True)
    report.setdefault("outcome", {})["status"] = status
    return report


def _result(job: dict) -> dict:
    cfg, status = job["cfg"], job["status"]
    tier = {"PASSED": "T2", "REJECTED": "T0"}.get(status, "T1")
    s = job["S"] if status == "PASSED" else None
    problems = []
    if status == "REJECTED":
        problems = [{"code": cfg.get("failure_code", "NPU_PORT_MISMATCH"), "fault": "miner",
                     "public_reason": "MOCK: the mock configuration rejected this design"}]
    elif status in ("INCOMPLETE", "ERROR"):
        problems = [{"code": "MOCK_SYSTEM_FAULT", "fault": "system", "public_reason": "MOCK: simulated system fault"}]
    if cfg.get("projection") == "random":
        projection = secrets.token_hex(32)
    else:
        projection = hashlib.sha256("|".join([job["digest"], job["chain_commit"] or "", status,
                                              repr(s)]).encode()).hexdigest()
    return {
        "schema": "cf-response/1", "mock": True, "request_id": job["request_id"], "mode": job["mode"],
        "status": status, "tier_reached": tier, "summary": f"MOCK result ({status}); no EDA tools ran",
        "problems": problems, "score": None, "reward_eligible": None,
        "submission": {"submitter": job["submitter"], "received_utc": job["received_utc"],
                       "commit_sha256": job["digest"]},
        "evaluator_sha256": MOCK_EVALUATOR_SHA,
        "projection_sha256": projection,
        "receipt": {"tier": tier, "sha256": projection, "path": None},
        "public_report": _report(status, s, cfg, tier),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _job_response(job: dict, accepted: bool = False) -> JSONResponse:
    """accepted=True: a new job, which (like the real server) always gets 202 first."""
    headers = {"X-Request-ID": job["request_id"], "X-CF-Evaluator-Sha256": MOCK_EVALUATOR_SHA,
               "Location": "/v2/status/" + job["request_id"], "Cache-Control": "no-store"}
    if not accepted and time.monotonic() >= job["ready_at"]:
        if job["result"] is None:
            job["result"] = _result(job)
            print(f"[MOCK] {job['request_id']}: {job['status']} S={job['S'] if job['status'] == 'PASSED' else '-'} "
                  f"(no EDA tools ran)", flush=True)
        return JSONResponse(job["result"], status_code=200, headers=headers)
    headers["Retry-After"] = "5"
    return JSONResponse({"request_id": job["request_id"], "evaluator_sha256": MOCK_EVALUATOR_SHA, "phase": "running",
                         "status_url": "/v2/status/" + job["request_id"], "mode": job["mode"]},
                        status_code=202, headers=headers)


@router.get("/health")
async def health():
    return {"ready": True, "status": "ok", "active_evaluator": MOCK_EVALUATOR_SHA, "modes": list(MODES),
            "mock": True, "install_failures": []}


@router.post("/evaluate")
async def evaluate(request: Request):
    try:
        form = await request.form()
    except Exception:
        return _err(415, "MULTIPART_REQUIRED", "Use multipart/form-data with design_zip, mode and request_id.")
    upload = form.get("design_zip")
    mode, rid = form.get("mode"), form.get("request_id")
    if upload is None or not hasattr(upload, "read") or not mode or not rid:
        return _err(400, "MISSING_FIELD", "design_zip, mode and request_id are required.")
    if not ID.match(str(rid)):
        return _err(400, "INVALID_ID", "Invalid request ID.")
    if mode not in MODES:
        return _err(400, "MODE_DISABLED", f"Choose one of {', '.join(MODES)}.")
    sha = str(form.get("evaluator_sha256") or "")
    if sha and sha != MOCK_EVALUATOR_SHA:
        return _err(404, "EVALUATOR_NOT_CACHED", "This evaluator is not installed (mock: use /v2/health's active_evaluator).")
    data = await upload.read()
    digest = hashlib.sha256(data).hexdigest()
    raw_cc = form.get("chain_commit")
    if raw_cc is None and mode in ("T1", "T2"):
        return _err(400, "CHAIN_COMMIT_REQUIRED", "T1/T2 require chain_commit (cf-chain-commit/1).")
    chain = None
    if raw_cc is not None:
        try:
            chain = json.dumps(_check_chain_commit(str(raw_cc), digest), sort_keys=True, separators=(",", ":"))
        except ValueError as e:
            return _err(400, "INVALID_CHAIN_COMMIT", str(e))
    old = _jobs.get(rid)
    if old is not None:
        if (old["digest"], old["chain_commit"], old["mode"]) != (digest, chain, mode):
            return _err(409, "REQUEST_ID_CONFLICT", "This request ID already names different bytes, mode or chain_commit.")
        return _job_response(old)
    cfg = {**_config(), **_design_overrides(data)}
    status = str(cfg.get("status", "PASSED")).upper()
    if status not in STATUSES:
        status = "PASSED"
    s = round(_score(cfg.get("S", [1.0, 6.0]), digest) + float(cfg.get("S_offset", 0) or 0), 6)
    delay = max(0.0, float(cfg.get("delay_seconds", 10)))
    job = {"request_id": rid, "mode": mode, "digest": digest, "chain_commit": chain, "cfg": cfg,
           "submitter": str(form.get("submitter") or rid), "status": status, "S": s,
           "received_utc": datetime.now(timezone.utc).isoformat(), "ready_at": time.monotonic() + delay,
           "result": None}
    _jobs[rid] = job
    print(f"[MOCK] accepted {rid} ({len(data)} bytes, {mode}, chain_commit {'yes' if chain else 'no'}): "
          f"{status} in {delay:.0f}s", flush=True)
    return _job_response(job, accepted=True)


@router.get("/status/{rid}")
async def status(rid: str):
    job = _jobs.get(rid) if ID.match(rid) else None
    if job is None:
        return _err(404, "NOT_FOUND", "Unknown request ID.")
    return _job_response(job)


@router.get("/evaluator")
async def evaluators():
    return {"evaluators": [{"evaluator_sha256": MOCK_EVALUATOR_SHA, "phase": "ready", "mock": True}],
            "active": MOCK_EVALUATOR_SHA}
