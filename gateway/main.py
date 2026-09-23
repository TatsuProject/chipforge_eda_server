import zipfile, tempfile, json, aiohttp, asyncio, aiofiles, os
import importlib.util
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import secrets
# THE SHARED SECRET. There was a comment saying "EDA_API_KEY comes from .env" and no code anywhere
# that read it. The gateway accepts an evaluator zip from the caller and verilator-api executes the
# run.py inside it as root, so an open :8080 is remote code execution for anyone who can reach it.
#
# If EDA_API_KEY is set, every /evaluate must carry it in X-API-Key, compared in constant time. If it
# is NOT set the gateway still serves -- with a loud warning at startup -- because the subnet team is
# mid-integration and a silent refusal would read as a broken server. Fail-closed is the go-live
# setting: set the variable. Audit 2026-09-23.
import os
import secrets as _secrets
from fastapi import Header, HTTPException, Depends
_API_KEY = (os.environ.get("EDA_API_KEY") or "").strip()
if not _API_KEY:
    print("[gateway] WARNING: EDA_API_KEY is not set. /evaluate is UNAUTHENTICATED. Set it before "
          "exposing this port to anything but the validator.", flush=True)


async def require_api_key(x_api_key: str = Header(default=None)):
    if _API_KEY and not (x_api_key and _secrets.compare_digest(x_api_key, _API_KEY)):
        raise HTTPException(status_code=401, detail="missing or wrong X-API-Key")


app = FastAPI(title="ChipForge EDA Tools Gateway", version="5.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)  # no free schema for a scanner

# Service URLs (must match docker-compose ports)
VERILATOR_API = "http://verilator-api:8001/simulate_and_evaluate"
OPENLANE_API  = "http://openlane-api:8003/run_openlane"


# -------------------------------
# Helpers
# -------------------------------
def _unzip(src_zip: Path, dst_dir: Path):
    with zipfile.ZipFile(src_zip, "r") as zf:
        zf.extractall(dst_dir)

def _rezip(src_dir: Path, out_zip: Path):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src_dir.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(src_dir))
    return out_zip


def generate_submission_id(length: int = 32) -> str:
    """
    Generate a unique submission ID
    
    Args:
        length: Length of the hex string (default 32 characters)
    
    Returns:
        Hex string submission ID
    """
    id_length_bytes = length // 2  # Convert hex chars to bytes
    submission_id = secrets.token_hex(id_length_bytes)
    return submission_id


def compute_weighted_score(func_0_1, area_um2, ips, power_mw, weights, targets, v_details=None, fmax_mhz=None):
    """
    Compute weighted score with functionality, area, performance, and power.
    
    Args:
        func_0_1: Functionality score (0.0 to 1.0)
        area_um2: Area in square micrometers
        ips: Instructions per second
        power_mw: Power in milliwatts
        weights: Dict with weights for each metric
        targets: Dict with target values and thresholds
    
    Returns:
        Dict with individual scores, overall score, and gate flags
    """
    # Sentinel: validator returns func_0_1 = -1.0 when the design's simulated
    # RTL does not match the synth view (e.g. `ifdef verilator hides logic from
    # OpenLane). The design is unusable, so every score is forced to -1.0
    # rather than letting the weighted sum land at e.g. -70 from a single
    # negative term. -1 is out of the legitimate [0,1]*100=[0,100] range, so
    # the UI can render it as a clear rejection signal.
    if func_0_1 is not None and func_0_1 < 0:
        return {
            "func_score": -1.0,
            "area_score": -1.0,
            "perf_score": -1.0,
            "power_score": -1.0,
            "overall": -1.0,
            "functional_gate": False,
            "overall_gate": False,
        }

    # ---- Challenge 15 gated FoM-RealMacro scoring (opt-in via targets.scoring_mode) ----
    # area_um2 IS the WHOLE synthesized SoC (core + accelerator logic + memory macros, from the one
    # rtl.f the design is simulated with) — the core's area is inside it, never a separate floor.
    # GATES: func must be 100% (bit-exact); perf must beat baseline by >= min_speedup. Then a
    # scale-invariant FoM = IPS^alpha / area_total^beta (perf-primary, area can't -> 0). See analysis/15.
    if str(targets.get("scoring_mode", "")) == "fom_realmacro":
        func_threshold = float(targets.get("func_threshold", 1.0))
        alpha     = float(targets.get("alpha", 0.7))
        beta      = float(targets.get("beta", 0.3))
        min_speedup = float(targets.get("min_speedup", 2.0))
        perf_ref  = float(targets.get("perf_target_ips", 1.0))
        func_gate = (func_0_1 is not None) and (func_0_1 >= func_threshold)
        # PERF GATE: geomean of PER-MODEL speedups, each model vs its OWN no-accel baseline.
        # The evaluator reports cycle_speedup_geomean = geomean_m(baseline_cyc_m / measured_cyc_m);
        # the true inferences/sec speedup = that cycle ratio x (submission_fmax / baseline_fmax)
        # (the cycle ratio is per-model, the clock ratio is model-independent). Dividing a multi-model
        # geomean IPS by ONE model's baseline would wrongly reject good accelerators on big models.
        # Falls back to the single-reference ips/perf_target_ips when the evaluator doesn't report it.
        csg = (v_details or {}).get("cycle_speedup_geomean")
        base_fmax = float(targets.get("baseline_fmax_mhz", 0.0))
        if csg and base_fmax > 0 and fmax_mhz:
            speedup = float(csg) * (fmax_mhz / base_fmax)
        else:
            speedup = (ips / perf_ref) if (ips and perf_ref > 0) else 0.0
        # Round ONCE, then gate on the rounded value. The response reports speedup to 3 decimals, so
        # gating on the raw float could return "speedup 2.0, min_speedup 2.0, REJECTED" for a design
        # at 1.9996 -- a verdict the miner cannot reconcile with the numbers they were shown.
        speedup = round(speedup, 3)
        perf_gate = speedup >= min_speedup
        area_total = area_um2 or 0.0
        # The FoM is a MEASUREMENT of what was measured, so it is computed whenever the inputs exist
        # -- NOT only when the gates pass. It used to be zeroed on any gate failure, which threw the
        # number away: a submission 4% short of the perf gate was told "overall 0.0" and learned
        # nothing about how close it came or what it would have scored. The verdict is carried by
        # overall_gate (and by result: ACCEPTED/REJECTED, which reads it), so the number does not
        # have to carry it too.
        measurable = bool(area_total > 0 and ips)
        # THE NUMERATOR IS WHAT THE ACCELERATOR ADDS, not the whole SoC's throughput.
        #
        # area_scope is npu_only, so the denominator prices the accelerator alone -- but raw IPS in
        # the numerator is the whole SoC's speed, most of which the CPU delivers with no accelerator
        # at all. So a do-nothing npu paid almost no area and collected the baseline's IPS for free:
        # measured, the inert stub (2,613 um2, 1.000x) scored 90.80 and the real 16x16 accelerator
        # (4.99 M um2, 1.949x) scored 15.02. The gate hid it, because a stub cannot reach 2x -- but
        # the number read as a ranking and ranked backwards.
        #
        # ips_gain = ips - ips_baseline = ips x (1 - 1/speedup). A design that adds nothing scores
        # exactly zero; the ranking among designs that DO accelerate still trades throughput against
        # area at the same alpha and beta. Decided 2026-09-23 before the npu-v1.0 launch.
        ips_gain = ips * (1.0 - 1.0 / speedup) if (measurable and speedup > 1.0) else 0.0
        fom = (ips_gain ** alpha) / (area_total ** beta) if ips_gain > 0 else 0.0
        passed = bool(func_gate and perf_gate and measurable)
        overall = round(fom * 1000.0, 4)      # FoM*1000 for readable magnitude; ranking is unaffected
        return {
            "func_score": round((func_0_1 or 0.0) * 100, 2),
            "area_total_um2": round(area_total, 2),
            # Read from the evaluator's own knobs. This used to be a hardcoded "whole_soc: core +
            # accelerator + memory macros" string, which contradicted openlane_results.area_scope
            # in the same response the moment the scope changed to npu_only.
            "area_scope": str(targets.get("area_scope", "unspecified")),
            "speedup_vs_baseline": round(speedup, 3),
            "min_speedup": min_speedup,
            "perf_gate": perf_gate,
            "functional_gate": func_gate,
            "overall": overall,
            "fom_raw": fom,
            "overall_gate": passed,
            # What a validator should weight on. Gating belongs in ONE place, and it is this field
            # plus overall_gate -- never in the measurement.
            "overall_gated": overall if passed else 0.0,
            "overall_note": (
                "overall is the measured FoM x 1000 whether or not the gates passed, so a rejected "
                "submission can see how close it was. Weight on overall_gated, which is 0.0 unless "
                "overall_gate is true. A FoM reported next to functional_gate=false is NOT a score: "
                "a design that returns wrong answers quickly has a high FoM."),
            "scoring_mode": "fom_realmacro",
        }

    # Extract thresholds and targets
    func_threshold    = float(targets.get("func_threshold", 0.90))
    overall_threshold = float(targets.get("overall_threshold", 0.0))
    area_ref          = float(targets.get("area_target_um2", 1.0))
    perf_ref          = float(targets.get("perf_target_ips", 1.0))
    power_ref         = float(targets.get("power_target_mw", 1.0))
    ratio_cap         = float(targets.get("ratio_cap", 2.0))
    
    # Extract weights
    w_functionality = float(weights.get("functionality", 0.5))
    w_area          = float(weights.get("area", 0.25))
    w_perf          = float(weights.get("performance", 0.25))
    w_power         = float(weights.get("power", 0.0))

    # --- Calculate component scores ---
    # Functionality: direct score (no threshold subtraction for scoring)
    func_component = func_0_1
    
    # Area: smaller is better (inverted ratio)
    area_component = min(area_ref / area_um2, ratio_cap) if area_um2 and area_ref > 0 else 0.0
    
    # Performance: higher is better
    perf_component = min(ips / perf_ref, ratio_cap) if ips and perf_ref > 0 else 0.0
    
    # Power: lower is better (inverted ratio)
    power_component = min(power_ref / power_mw, ratio_cap) if power_mw and power_ref > 0 else 0.0

    # --- Calculate weighted overall score ---
    total_w = w_functionality + w_area + w_perf + w_power
    if total_w <= 0:
        total_w = 1.0

    overall = (
        w_functionality * func_component +
        w_area * area_component +
        w_perf * perf_component +
        w_power * power_component
    ) / total_w

    # --- Gates: check if thresholds are met (for flags only, not for zeroing scores) ---
    func_gate    = func_0_1 >= func_threshold
    overall_gate = overall >= overall_threshold

    # --- Return all scores and flags ---
    return {
        "func_score": round(func_0_1 * 100, 2),
        "area_score": round(area_component * 100, 2),
        "perf_score": round(perf_component * 100, 2),
        "power_score": round(power_component * 100, 2),
        "overall": round(overall * 100, 2),
        "functional_gate": func_gate,
        "overall_gate": overall_gate
    }

async def make_http_request(session: aiohttp.ClientSession, url: str, files_data: dict):
    """Make async HTTP request with multipart files"""
    data = aiohttp.FormData()
    for field_name, value in files_data.items():
        if field_name == "submission_id":
            # Add submission_id as a form field, not a file
            data.add_field(field_name, value)
        else:
            # Add file fields
            data.add_field(field_name, open(value, 'rb'), filename=value.name)
    
    async with session.post(url, data=data) as response:
        # The status was never read. A routed 404/405/422 -- a wrong path, a renamed field, a
        # validation error from FastAPI itself -- carries a JSON body, which came back here as the
        # service's "response" and was then classified by the miner-or-system rules as if it were an
        # evaluation. It is neither: it is the request never having run. Ours, retryable.
        if response.status != 200:
            body = (await response.text())[:400]
            return {"success": False,
                    "error": {"code": "SERVICE_HTTP_%d" % response.status, "category": "system",
                              "fault": "system", "retryable": True,
                              "message": f"{url} answered HTTP {response.status}: {body}"}}
        return await response.json()


# -------------------------------
# Upload-layer validation (so a miner always knows WHERE their submission failed)
# -------------------------------
MAX_SUBMISSION_BYTES   = int(os.environ.get("C15_MAX_SUBMISSION_MB", "300")) * 1024 * 1024  # compressed upload cap
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("C15_MAX_UNCOMPRESSED_MB", "4096")) * 1024 * 1024  # zip-bomb guard

def _as_error(e):
    """Whatever a service left under `error`, as a structured dict -- or None if there is no error.

    Services are supposed to return {code, category, fault, retryable, message}. Two of them do not
    always: openlane-api reports unstructured `error_message` on all four of its failure paths, and
    an aiohttp exception used to be stringified straight into this field. The code below asks every
    error for .get("fault"), so a bare string was an AttributeError -- which the outer `except`
    turned into a response with no result and no fault, discarding an evaluation that had already
    cost most of an hour.

    An error we cannot parse is OURS by default. A service that failed in a way it could not
    describe is not evidence about the submission."""
    if isinstance(e, dict):
        return e or None
    if e:
        return {"code": "SERVICE_UNAVAILABLE", "category": "system", "fault": "system",
                "retryable": True, "message": str(e)}
    return None


def _miner_reject(submission_id, code, category, stage, message, detail=""):
    return JSONResponse({
        "success": True, "submission_id": submission_id, "result": "REJECTED",
        "functionality_score": 0.0,
        "error": {"code": code, "category": category, "fault": "miner", "stage": stage,
                  "message": message, "detail": (detail or "")[-1500:], "retryable": False},
        "final_score": {"overall": 0.0, "overall_gate": False, "scored": True}})

def _system_error(submission_id, code, stage, message, detail="", retryable=False):
    return JSONResponse({
        "success": False, "submission_id": submission_id, "result": "ERROR",
        "error": {"code": code, "category": "system", "fault": "system", "stage": stage,
                  "message": message, "detail": (detail or "")[-1500:], "retryable": retryable},
        "final_score": {"overall": None, "overall_gate": False, "scored": False}})

def _zip_problem(path):
    """Structural validation of a submission zip. Returns (code, message, detail) | None (all miner-fault)."""
    sz = path.stat().st_size
    if sz == 0:
        return ("SUBMISSION_EMPTY", "The submission archive is empty (0 bytes).", "")
    if sz > MAX_SUBMISSION_BYTES:
        return ("SUBMISSION_TOO_LARGE", f"The submission ({sz//(1024*1024)} MB) exceeds the {MAX_SUBMISSION_BYTES//(1024*1024)} MB upload limit.", "")
    if not zipfile.is_zipfile(path):
        return ("SUBMISSION_NOT_A_ZIP", "The submission is not a valid .zip archive (corrupt or wrong format).", "")
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if not infos:
                return ("SUBMISSION_EMPTY", "The submission archive contains no files.", "")
            total = 0
            for zi in infos:
                n = zi.filename
                if n.startswith("/") or n.startswith("\\") or ".." in Path(n).parts or (len(n) > 1 and n[1] == ":"):
                    return ("SUBMISSION_UNSAFE_PATH", f"The submission contains an unsafe path (absolute or '..'): {n}", "")
                total += zi.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    return ("SUBMISSION_TOO_LARGE", "The submission expands beyond the allowed uncompressed size (possible zip bomb).", "")
    except zipfile.BadZipFile:
        return ("SUBMISSION_NOT_A_ZIP", "The submission archive is corrupt and could not be read.", "")
    return None

# -------------------------------
# Endpoint
# -------------------------------
@app.post("/evaluate", dependencies=[Depends(require_api_key)])
async def evaluate(
    design_zip: UploadFile = File(..., description="This is miner's submission"),
    evaluator_zip: UploadFile = File(..., description="Testcases downloaded when the challenge started"),
    submission_id: str = Form(None)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            if not submission_id:
                submission_id = generate_submission_id(length=32)
            work = Path(tmpd)

            # save zips using aiofiles
            design_path = work / "design.zip"
            async with aiofiles.open(design_path, 'wb') as f:
                await f.write(await design_zip.read())
            
            eval_path = work / "evaluator.zip"
            async with aiofiles.open(eval_path, 'wb') as f:
                await f.write(await evaluator_zip.read())

            # ---- upload-layer validation: the miner's submission zip (miner-fault) ----
            prob = _zip_problem(design_path)
            if prob:
                return _miner_reject(submission_id, prob[0], "submission", "intake", prob[1], prob[2])

            # ---- the evaluator bundle is OURS: a problem here is a system fault ----
            if not zipfile.is_zipfile(eval_path):
                return _system_error(submission_id, "EVALUATOR_BUNDLE_MALFORMED", "intake",
                                     "The evaluator bundle is not a valid zip.")
            eval_dir = work / "evaluator"
            eval_dir.mkdir()
            try:
                _unzip(eval_path, eval_dir)
            except Exception as e:
                return _system_error(submission_id, "EVALUATOR_BUNDLE_MALFORMED", "intake",
                                     "The evaluator bundle could not be extracted.", str(e))

            verilator_dir = eval_dir / "verilator"
            openlane_dir  = eval_dir / "openlane"
            gateway_dir   = eval_dir / "gateway"

            if not verilator_dir.exists() or not openlane_dir.exists():
                return _system_error(submission_id, "EVALUATOR_BUNDLE_MALFORMED", "intake",
                                     "The evaluator bundle is missing verilator/ or openlane/.")

            verilator_bundle = work / "verilator_bundle.zip"
            openlane_bundle  = work / "openlane_bundle.zip"
            _rezip(verilator_dir, verilator_bundle)
            _rezip(openlane_dir, openlane_bundle)

            # weights + targets
            # FAIL CLOSED. With weights.json absent this used to fall through to the legacy path with
            # overall_threshold 0.0 and no gates, and score EVERYTHING ACCEPTED -- including a design
            # returning wrong answers (the audit's refuter executed it: ACCEPTED at 50.0 and at 5.0).
            # Every evaluator bundle that has ever shipped carries this file, legacy ones included,
            # so its absence is a broken bundle, never a scoring mode.
            wj = gateway_dir / "weights.json"
            try:
                cfg = json.loads(wj.read_text())
                weights = cfg.get("weights", {})
                targets = cfg.get("targets", {})
                if not isinstance(weights, dict) or not isinstance(targets, dict):
                    raise ValueError("weights/targets are not objects")
            except (OSError, ValueError) as e:
                return _system_error(submission_id, "EVALUATOR_BUNDLE_MALFORMED", "intake",
                                     "Internal: the evaluator bundle's gateway/weights.json is missing or "
                                     "unreadable, so the submission cannot be scored.", str(e))

            # Use aiohttp session for parallel requests
            submission_files = {
                "design_zip": design_path,
                "verilator_bundle": verilator_bundle,
                "submission_id": submission_id
            }
            
            openlane_files = {
                "design_zip": design_path,
                "openlane_bundle": openlane_bundle,
                "submission_id": submission_id
            }
            # How long the gateway waits for both services. Configurable because it is a
            # DEPLOYMENT property, not a constant: synthesis dominates an evaluation and scales
            # with the submitted design, and openlane-api serialises synthesis behind a
            # Semaphore(1), so N concurrent evaluations queue N synthesis runs end to end. A box
            # taking four at once needs a different ceiling than one taking them singly.
            timeout = aiohttp.ClientTimeout(total=int(os.environ.get("EDA_REQUEST_TIMEOUT_S", "2700")))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                
                # Always call Verilator
                verilator_task = make_http_request(
                    session, 
                    VERILATOR_API, 
                    submission_files
                )

                # Conditionally call OpenLane
                tasks = [verilator_task]
                run_openlane = weights.get("area", 0) > 0 or weights.get("performance", 0) > 0
                
                if run_openlane:
                    openlane_task = make_http_request(
                        session,
                        OPENLANE_API,
                        openlane_files
                    )
                    tasks.append(openlane_task)
                
                # Execute requests in parallel
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Process Verilator results
                v_json = results[0] if not isinstance(results[0], Exception) else {"success": False, "error": str(results[0])}
                
                func_score = 0.0
                ipc, fmax_mhz, ips = None, None, None
                if v_json.get("success"):
                    v_res = v_json.get("results", {})
                    func_score = v_res.get("functionality_score", 0.0)
                    ipc        = v_res.get("details", {}).get("ipc")
                    fmax_mhz   = None  # Fmax will come from OpenLane
                
                # Process OpenLane results
                area_um2, fmax_mhz, power_mw = None, None, None
                if run_openlane and len(results) > 1:
                    # A structured error, not a bare string. `error` is read below as a dict with
                    # .get("fault"); putting a string here made that an AttributeError that threw
                    # away the whole evaluation -- including a verilator arm that had already
                    # succeeded after fifty minutes.
                    o_json = results[1] if not isinstance(results[1], Exception) else {
                        "success": False,
                        "error": {"code": "SERVICE_UNAVAILABLE", "category": "system",
                                  "fault": "system", "retryable": True,
                                  "message": f"openlane-api request failed: {results[1]}"}}
                    if o_json.get("success"):
                        area_um2 = o_json.get("results", {}).get("area_um2")
                        fmax_mhz = o_json.get("results", {}).get("fmax_mhz")
                        power_mw = o_json.get("results", {}).get("power_mw") # added for power
                else:
                    o_json = {"skipped": True}

            # ---- Challenge-15 structured error handling (opt-in: responses carry error.fault) ----
            # A SYSTEM fault (ours: toolchain/bundle/infra) must NEVER score the miner 0 -> return ERROR,
            # not scored, so the validator retries/alerts instead of setting weights. A MINER fault
            # (bad submission / failed gate) -> REJECTED, score 0, with the precise reason surfaced.
            v_res  = v_json.get("results", {}) if isinstance(v_json, dict) else {}
            v_err  = _as_error(v_res.get("error") if isinstance(v_res, dict) else None)
            o_res  = o_json.get("results", o_json) if isinstance(o_json, dict) else {}
            o_err  = _as_error(o_res.get("error") if isinstance(o_res, dict) else None)
            sys_err = next((e for e in (v_err, o_err) if e and e.get("fault") == "system"), None)
            if v_json.get("success") is not True and not v_err:
                sys_err = sys_err or {"code": "SERVICE_UNAVAILABLE", "category": "system", "fault": "system",
                                      "message": v_json.get("error") or v_json.get("error_message") or "verilator-api failed",
                                      "retryable": True}
            # The same guard for openlane, which did not have one. Without it a synthesis container
            # that OOMed, restarted or timed out in its own queue fell straight through to scoring:
            # area_um2 stays None, so measurable is False, so the response is REJECTED with
            # overall_gated 0.0 and `scored` true -- our infrastructure failure, charged to the miner
            # as a rejection. openlane-api returns unstructured error_message on every one of its
            # failure paths, so this is the ONLY thing standing between that and a zero.
            if isinstance(o_json, dict) and o_json.get("success") is not True and not o_err:
                sys_err = sys_err or {"code": "SERVICE_UNAVAILABLE", "category": "system", "fault": "system",
                                      "message": o_json.get("error") or o_json.get("error_message") or "openlane-api failed",
                                      "retryable": True}
            if func_score is None and not sys_err:
                sys_err = {"code": "INTERNAL_ERROR", "category": "system", "fault": "system",
                           "message": "evaluator returned no functionality score", "retryable": True}
            if sys_err:
                return {"success": False, "submission_id": submission_id, "result": "ERROR",
                        "error": sys_err, "verilator_results": v_json, "openlane_results": o_json,
                        "weights": weights, "targets": targets,
                        "final_score": {"overall": None, "overall_gate": False, "scored": False}}
            miner_err = next((e for e in (v_err, o_err) if e and e.get("fault") == "miner"), None)

            # ---- compute IPS ----
            # inferences/sec = ipc * fmax, where ipc = 1/cycles_per_inference and fmax is the REAL achievable
            # clock COMPUTED AT RUN TIME from the timing report: the area flow sets a nominal clock (e.g. 10 ns)
            # purely so STA reports a slack, then fmax = 1/(nominal_period - slack). A design with positive slack
            # closes faster than nominal; negative slack closes slower. This is the Challenge_0014 method — the
            # clock is NOT hardcoded into the score. (NOTE: today fmax comes from a synthesis-stage STA, which is
            # pessimistic vs post-place-and-route; accuracy/slack-source is an open decision — see analysis/17 §3b.)
            if ipc and fmax_mhz:
                ips = ipc * (fmax_mhz * 1e6)

            # ISA instructions/sec is COMPUTED in the verilator bundle (details.isa_ipc) but deliberately
            # NOT reported as a metric here — the score is the AI inferences/sec FoM. Available if needed.

            # ---- final score ----
            score = compute_weighted_score(
                func_0_1=func_score,
                area_um2=area_um2,
                ips=ips,
                power_mw=power_mw,  # added for power
                weights=weights,
                targets=targets,
                v_details=v_res.get("details", {}) if isinstance(v_res, dict) else {},
                fmax_mhz=fmax_mhz
            )

            resp = {
                "success": True,
                "submission_id": submission_id,
                "result": "ACCEPTED" if score.get("overall_gate") else "REJECTED",
                "verilator_results": v_json,
                "openlane_results": o_json,
                "weights": weights,
                "targets": targets,
                "final_score": score
            }
            if miner_err and not score.get("overall_gate"):
                resp["error"] = miner_err   # surface the precise reason (failed gate / bad accel RTL)
            return resp

    except Exception as e:
        return {
            "success": False, 
            "submission_id": submission_id if 'submission_id' in locals() else "",
            "error_message": str(e)
        }
