import zipfile, tempfile, json, aiohttp, asyncio, aiofiles
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


def compute_weighted_score(func_0_1, area_um2, ips, power_mw, weights, targets):
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

            # extract evaluator (keep sync - zipfile operations are fast)
            eval_dir = work / "evaluator"
            eval_dir.mkdir()
            _unzip(eval_path, eval_dir)

            verilator_dir = eval_dir / "verilator"
            openlane_dir  = eval_dir / "openlane"
            gateway_dir   = eval_dir / "gateway"

            if not verilator_dir.exists() or not openlane_dir.exists():
                return JSONResponse(
                    {"success": False, "error_message": "Missing verilator/ or openlane/ in evaluator.zip"},
                    status_code=400
                )

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
                area_um2, fmax_mhz = None, None
                if run_openlane and len(results) > 1:
                    o_json = results[1] if not isinstance(results[1], Exception) else {"success": False, "error": str(results[1])}
                    if o_json.get("success"):
                        area_um2 = o_json.get("results", {}).get("area_um2")
                        fmax_mhz = o_json.get("results", {}).get("fmax_mhz")
                        power_mw = o_json.get("results", {}).get("power_mw") # added for power
                else:
                    o_json = {"skipped": True}

            # ---- compute IPS ----
            if ipc and fmax_mhz:
                ips = ipc * (fmax_mhz * 1e6)  # instr per second

            # ---- final score ----
            score = compute_weighted_score(
                func_0_1=func_score,
                area_um2=area_um2,
                ips=ips,
                power_mw=power_mw,  # added for power
                weights=weights,
                targets=targets
            )

            return {
                "success": True,
                "submission_id": submission_id,
                "verilator_results": v_json,
                "openlane_results": o_json,
                "weights": weights,
                "targets": targets,
                "final_score": score
            }

    except Exception as e:
        return {
            "success": False, 
            "submission_id": submission_id if 'submission_id' in locals() else "",
            "error_message": str(e)
        }
