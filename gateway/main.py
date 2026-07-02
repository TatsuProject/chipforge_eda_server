import zipfile, tempfile, json, aiohttp, asyncio, aiofiles, os
import importlib.util
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import secrets
app = FastAPI(title="ChipForge EDA Tools Gateway", version="5.0.0")

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
    # area_total = core_area_um2 (fixed locked-core floor) + area_um2 (accelerator incl. macros).
    # GATES: func must be 100% (bit-exact); perf must beat baseline by >= min_speedup. Then a
    # scale-invariant FoM = IPS^alpha / area_total^beta (perf-primary, area can't -> 0). See analysis/15.
    if str(targets.get("scoring_mode", "")) == "fom_realmacro":
        func_threshold = float(targets.get("func_threshold", 1.0))
        alpha     = float(targets.get("alpha", 0.7))
        beta      = float(targets.get("beta", 0.3))
        core_area = float(targets.get("core_area_um2", 0.0))
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
        perf_gate = speedup >= min_speedup
        area_total = core_area + (area_um2 or 0.0)
        passed = bool(func_gate and perf_gate and area_total > 0 and ips)
        fom = (ips ** alpha) / (area_total ** beta) if passed else 0.0
        return {
            "func_score": round((func_0_1 or 0.0) * 100, 2),
            "area_total_um2": round(area_total, 2),
            "accelerator_area_um2": round(area_um2 or 0.0, 2),
            "core_area_um2": core_area,
            "speedup_vs_baseline": round(speedup, 3),
            "perf_gate": perf_gate,
            "functional_gate": func_gate,
            "overall": round(fom * 1000.0, 4),   # FoM*1000 for readable magnitude; ranking is scale-invariant
            "fom_raw": fom,
            "overall_gate": passed,
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
        return await response.json()


# -------------------------------
# Upload-layer validation (so a miner always knows WHERE their submission failed)
# -------------------------------
MAX_SUBMISSION_BYTES   = int(os.environ.get("C15_MAX_SUBMISSION_MB", "300")) * 1024 * 1024  # compressed upload cap
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("C15_MAX_UNCOMPRESSED_MB", "4096")) * 1024 * 1024  # zip-bomb guard

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
@app.post("/evaluate")
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
            weights, targets = {}, {}
            if (gateway_dir / "weights.json").exists():
                cfg = json.loads((gateway_dir / "weights.json").read_text())
                weights = cfg.get("weights", {})
                targets = cfg.get("targets", {})

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
            timeout = aiohttp.ClientTimeout(total=2700)  # 45 minute timeout
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
                    o_json = results[1] if not isinstance(results[1], Exception) else {"success": False, "error": str(results[1])}
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
            v_err  = v_res.get("error") if isinstance(v_res, dict) else None
            o_res  = o_json.get("results", o_json) if isinstance(o_json, dict) else {}
            o_err  = o_res.get("error") if isinstance(o_res, dict) else None
            sys_err = next((e for e in (v_err, o_err) if e and e.get("fault") == "system"), None)
            if v_json.get("success") is False and not v_err:
                sys_err = sys_err or {"code": "SERVICE_UNAVAILABLE", "category": "system", "fault": "system",
                                      "message": v_json.get("error") or v_json.get("error_message") or "verilator-api failed",
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
