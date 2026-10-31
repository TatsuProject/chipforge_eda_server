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

Evaluator installs (POST /v2/evaluator, GET /v2/evaluator/<sha>), as validators do them automatically:
  EDA_MOCK_INSTALL=simulated (default)  the install is simulated: the zip and .sshsig are checked for shape
                  (a MANIFEST.json, an SSH signature), then the phases verify, extract, fetch_assets,
                  post_install, preflight and ready follow over install_seconds (eda_mock.json, default 20);
                  install_fail (e.g. "EVALUATOR_PREFLIGHT_FAILED") makes it fail at preflight.
  EDA_MOCK_INSTALL=real   (hybrid: make hybrid-start) installs are REAL: forwarded to the evaluator service
                  (signature check against CF_V2_EVALUATOR_PUBLIC_KEY, ~1 GB of assets, post-install,
                  preflight); /v2/health is the real one. Only evaluations are simulated.
  Either way an evaluation names its evaluator (evaluator_sha256) and is refused unless that evaluator is
  installed and ready here, exactly like the real server.

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
INSTALL_MODE = os.environ.get("EDA_MOCK_INSTALL", "simulated").strip().lower()       # simulated | real
PHASES = ("verify", "extract", "fetch_assets", "post_install", "preflight")
MAX_UPLOAD_BYTES = int(float(os.environ.get("EDA_MOCK_MAX_UPLOAD_MB", "300")) * 1024 * 1024)
MAX_JOBS, MAX_INSTALLS = 5000, 20      # remembered jobs / simulated installs (oldest forgotten first)


def _too_large(request: Request):
    """Uploads are read into memory: refuse a body above EDA_MOCK_MAX_UPLOAD_MB before reading it."""
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size > MAX_UPLOAD_BYTES:
        return _err(413, "TOO_LARGE", f"Upload larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not request.headers.get("content-length"):
        return _err(411, "CONTENT_LENGTH_REQUIRED", "Provide one Content-Length (no chunked upload).")
    return None


def _forget_oldest(store: dict, keep: int, protect=()) -> None:
    for key in list(store)[:max(0, len(store) - keep)]:
        if key not in protect:
            store.pop(key, None)
REPORTS = Path(__file__).resolve().parent / "mock_reports"
ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CHAIN_FIELDS = {"schema", "subtensor_genesis_hash", "netuid", "hotkey", "block_number", "block_hash",
                "block_timestamp_ms", "package_sha256"}
MODES = ("T0", "T1", "T2", "DEV")
STATUSES = ("PASSED", "REJECTED", "INCOMPLETE", "ERROR")
_jobs: dict = {}
# sha256 -> {"started": monotonic, "seconds": install time, "fail": failure code or None, "order": n}.
# The default evaluator is installed from the start (the mock answers without any install).
_installs: dict = {MOCK_EVALUATOR_SHA: {"started": 0.0, "seconds": 0.0, "fail": None, "order": 0}}


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
        "evaluator_sha256": job["evaluator"],
        "projection_sha256": projection,
        "receipt": {"tier": tier, "sha256": projection, "path": None},
        "public_report": _report(status, s, cfg, tier),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _job_response(job: dict, accepted: bool = False) -> JSONResponse:
    """accepted=True: a new job, which (like the real server) always gets 202 first."""
    headers = {"X-Request-ID": job["request_id"], "X-CF-Evaluator-Sha256": job["evaluator"],
               "Location": "/v2/status/" + job["request_id"], "Cache-Control": "no-store"}
    if not accepted and time.monotonic() >= job["ready_at"]:
        if job["result"] is None:
            job["result"] = _result(job)
            print(f"[MOCK] {job['request_id']}: {job['status']} S={job['S'] if job['status'] == 'PASSED' else '-'} "
                  f"(no EDA tools ran)", flush=True)
        return JSONResponse(job["result"], status_code=200, headers=headers)
    headers["Retry-After"] = "5"
    return JSONResponse({"request_id": job["request_id"], "evaluator_sha256": job["evaluator"], "phase": "running",
                         "status_url": "/v2/status/" + job["request_id"], "mode": job["mode"]},
                        status_code=202, headers=headers)


def _install_phase(sha: str) -> dict:
    """Simulated install state of one evaluator, as GET /v2/evaluator/<sha> reports it."""
    rec = _installs[sha]
    elapsed = time.monotonic() - rec["started"]
    step = rec["seconds"] / len(PHASES) if rec["seconds"] else 0
    i = int(elapsed // step) if step else len(PHASES)
    if rec["fail"] and i >= PHASES.index("preflight"):
        return {"evaluator_sha256": sha, "phase": "failed", "attempt": 1, "error": rec["fail"], "mock": True}
    phase = PHASES[i] if i < len(PHASES) else "ready"
    return {"evaluator_sha256": sha, "phase": phase, "attempt": 1, "error": None, "mock": True}


def _ready_shas() -> list:
    return [sha for sha in sorted(_installs, key=lambda k: _installs[k]["order"])
            if _install_phase(sha)["phase"] == "ready"]


async def _real_state(sha: str):
    """Hybrid: the real evaluator service's install state (None when unreachable)."""
    import aiohttp
    from v2_proxy import _cfg
    base = _cfg()[0]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(f"{base}/evaluator/{sha}") as r:
                return await r.json(content_type=None) if r.status == 200 else {}
    except Exception:
        return None


@router.get("/health")
async def health(request: Request):
    if INSTALL_MODE == "real":
        from v2_proxy import _forward
        real = await _forward(request, "GET", "/health")
        try:
            body = json.loads(real.body)
        except ValueError:
            return real
        body.update(mock=True, evaluations="simulated (EDA_MOCK_FILE); installs are real")
        return JSONResponse(body, status_code=real.status_code)
    ready = _ready_shas()
    return {"ready": True, "status": "ok", "active_evaluator": ready[-1] if ready else MOCK_EVALUATOR_SHA,
            "modes": list(MODES), "mock": True, "install_failures": [
                {"evaluator_sha256": sha, "error": st["error"]} for sha in _installs
                for st in [_install_phase(sha)] if st["phase"] == "failed"]}


@router.post("/evaluator")
async def install(request: Request):
    from v2_proxy import _check_admin, _forward
    refused = _check_admin(request)
    if refused is not None:
        return refused
    if INSTALL_MODE == "real":
        return await _forward(request, "POST", "/evaluator", upload=True)
    too_large = _too_large(request)
    if too_large is not None:
        return too_large
    try:
        form = await request.form()
    except Exception:
        return _err(415, "MULTIPART_REQUIRED", "Use multipart/form-data with evaluator_zip and evaluator_sig.")
    upload, sig = form.get("evaluator_zip"), form.get("evaluator_sig") or form.get("signature")
    if upload is None or not hasattr(upload, "read") or sig is None:
        return _err(400, "MISSING_FIELD", "evaluator_zip and evaluator_sig are required.")
    data = await upload.read()
    sig_bytes = await sig.read() if hasattr(sig, "read") else str(sig).encode()
    sha = hashlib.sha256(data).hexdigest()
    claimed = str(form.get("evaluator_sha256") or "")
    if claimed and claimed != sha:
        return _err(400, "EVALUATOR_SHA256_MISMATCH", "evaluator_sha256 does not match the uploaded zip.")
    if not sig_bytes.strip().startswith(b"-----BEGIN SSH SIGNATURE-----"):
        return _err(400, "EVALUATOR_SIGNATURE_INVALID", "The signature is not an OpenSSH signature (mock check).")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            json.loads(z.read("MANIFEST.json"))
    except Exception:
        return _err(400, "EVALUATOR_MANIFEST_INVALID", "The evaluator zip has no readable MANIFEST.json.")
    cfg = _config()
    if sha not in _installs or _install_phase(sha)["phase"] == "failed":
        _installs[sha] = {"started": time.monotonic(), "seconds": max(0.0, float(cfg.get("install_seconds", 20))),
                          "fail": cfg.get("install_fail") or None,
                          "order": max((r["order"] for r in _installs.values()), default=0) + 1}
        _forget_oldest(_installs, MAX_INSTALLS, protect=(MOCK_EVALUATOR_SHA, sha))
        print(f"[MOCK] installing evaluator {sha[:16]} ({len(data)} bytes, simulated "
              f"{_installs[sha]['seconds']:.0f}s{', will fail: ' + _installs[sha]['fail'] if _installs[sha]['fail'] else ''})",
              flush=True)
    return JSONResponse(_install_phase(sha) | {"install_url": "/v2/evaluator/" + sha}, status_code=202,
                        headers={"Location": "/v2/evaluator/" + sha, "Retry-After": "5"})


@router.get("/evaluator/{sha}")
async def install_state(sha: str, request: Request):
    if INSTALL_MODE == "real":
        from v2_proxy import _forward
        return await _forward(request, "GET", "/evaluator/" + sha) if HEX64.match(sha) else \
            _err(404, "NOT_FOUND", "Unknown evaluator.")
    if sha not in _installs:
        return _err(404, "NOT_FOUND", "Unknown evaluator.")
    return _install_phase(sha)


@router.post("/evaluate")
async def evaluate(request: Request):
    too_large = _too_large(request)
    if too_large is not None:
        return too_large
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
    if INSTALL_MODE == "real":
        state = await _real_state(sha) if HEX64.match(sha) else {}
        if state is None:
            return _err(503, "V2_UNREACHABLE", "The v2 evaluator service is not reachable.")
        if state.get("phase") != "ready":
            return _err(404, "EVALUATOR_NOT_CACHED", f"Evaluator {sha[:16] or '(none)'} is not installed and ready "
                                                     f"here (phase {state.get('phase') or 'unknown'}).")
    else:
        sha = sha or (_ready_shas() or [MOCK_EVALUATOR_SHA])[-1]
        if sha not in _installs:
            return _err(404, "EVALUATOR_NOT_CACHED", "This evaluator is not installed (POST /v2/evaluator).")
        if _install_phase(sha)["phase"] != "ready":
            return _err(409, "EVALUATOR_NOT_READY", f"This evaluator is still installing "
                                                    f"({_install_phase(sha)['phase']}).")
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
        if (old["digest"], old["chain_commit"], old["mode"], old["evaluator"]) != (digest, chain, mode, sha):
            return _err(409, "REQUEST_ID_CONFLICT", "This request ID already names different bytes, mode or chain_commit.")
        return _job_response(old)
    cfg = {**_config(), **_design_overrides(data)}
    status = str(cfg.get("status", "PASSED")).upper()
    if status not in STATUSES:
        status = "PASSED"
    s = round(_score(cfg.get("S", [1.0, 6.0]), digest) + float(cfg.get("S_offset", 0) or 0), 6)
    delay = max(0.0, float(cfg.get("delay_seconds", 10)))
    job = {"request_id": rid, "mode": mode, "digest": digest, "chain_commit": chain, "cfg": cfg, "evaluator": sha,
           "submitter": str(form.get("submitter") or rid), "status": status, "S": s,
           "received_utc": datetime.now(timezone.utc).isoformat(), "ready_at": time.monotonic() + delay,
           "result": None}
    _jobs[rid] = job
    _forget_oldest(_jobs, MAX_JOBS)
    print(f"[MOCK] accepted {rid} ({len(data)} bytes, {mode}, chain_commit {'yes' if chain else 'no'}, "
          f"evaluator {sha[:12]}): {status} in {delay:.0f}s", flush=True)
    return _job_response(job, accepted=True)


@router.get("/status/{rid}")
async def status(rid: str):
    job = _jobs.get(rid) if ID.match(rid) else None
    if job is None:
        return _err(404, "NOT_FOUND", "Unknown request ID.")
    return _job_response(job)


@router.get("/evaluator")
async def evaluators(request: Request):
    if INSTALL_MODE == "real":
        from v2_proxy import _forward
        return await _forward(request, "GET", "/evaluator")
    ready = _ready_shas()
    return {"evaluators": [_install_phase(sha) for sha in _installs], "active": ready[-1] if ready else None}
