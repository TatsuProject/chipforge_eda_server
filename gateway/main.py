import zipfile, tempfile, json, aiohttp, asyncio, aiofiles
from pathlib import Path
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

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


def compute_weighted_score(func_0_1, area_um2, ips, weights, targets):
    func_threshold     = float(targets.get("func_threshold", 0.90))
    overall_threshold  = float(targets.get("overall_threshold", 0.0))  # renamed and added
    area_ref           = float(targets.get("area_target_um2", 1.0))
    perf_ref           = float(targets.get("perf_target_ips", 1.0))
    ratio_cap          = float(targets.get("ratio_cap", 2.0))

    w_functionality = float(weights.get("functionality", 0.5))
    w_area          = float(weights.get("area", 0.25))
    w_perf          = float(weights.get("performance", 0.25))

    # --- Functionality Gate ---
    func_raw = max(0.0, min(1.0, func_0_1))
    if func_raw < func_threshold:
        return {
            "func_score"     : round(func_raw * 100, 2),
            "area_score"     : 0.0,
            "perf_score"     : 0.0,
            "overall"        : 0.0,
            "functional_gate": False,
            "overall_gate"   : False
        }

    # --- Components ---
    func_component = (func_raw - func_threshold) / (1 - func_threshold) if func_threshold < 1 else 1.0
    area_component = min(area_ref / area_um2, ratio_cap) if area_um2 and area_ref > 0 else 0.0
    perf_component = min(ips / perf_ref, ratio_cap) if ips and perf_ref > 0 else 0.0

    total_w = w_functionality + w_area + w_perf
    if total_w <= 0:
        total_w = 1.0

    overall = (
        w_functionality * func_component +
        w_area * area_component +
        w_perf * perf_component
    ) / total_w

    # --- Overall Gate ---
    if overall < overall_threshold:
        return {
            "func_score"     : round(func_raw * 100, 2),
            "area_score"     : round(area_component * 100, 2),
            "perf_score"     : round(perf_component * 100, 2),
            "overall"        : 0.0,
            "functional_gate": True,
            "overall_gate"   : False
        }

    # --- Success ---
    return {
        "func_score"     : round(func_raw * 100, 2),
        "area_score"     : round(area_component * 100, 2),
        "perf_score"     : round(perf_component * 100, 2),
        "overall"        : round(overall * 100, 2),
        "functional_gate": True,
        "overall_gate"   : True
    }


async def make_http_request(session: aiohttp.ClientSession, url: str, files_data: dict):
    """Make async HTTP request with multipart files"""
    data = aiohttp.FormData()
    for field_name, file_path in files_data.items():
        data.add_field(field_name, open(file_path, 'rb'), filename=file_path.name)
    
    async with session.post(url, data=data) as response:
        return await response.json()


# -------------------------------
# Endpoint
# -------------------------------
@app.post("/evaluate")
async def evaluate(
    design_zip: UploadFile = File(...),
    evaluator_zip: UploadFile = File(...)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
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
            timeout = aiohttp.ClientTimeout(total=1800)  # 30 minute timeout
            async with aiohttp.ClientSession(timeout=timeout) as session:
                
                # Always call Verilator
                verilator_task = make_http_request(
                    session, 
                    VERILATOR_API, 
                    {"design_zip": design_path, "verilator_bundle": verilator_bundle}
                )
                
                # Conditionally call OpenLane
                tasks = [verilator_task]
                run_openlane = weights.get("area", 0) > 0 or weights.get("performance", 0) > 0
                
                if run_openlane:
                    openlane_task = make_http_request(
                        session,
                        OPENLANE_API,
                        {"design_zip": design_path, "openlane_bundle": openlane_bundle}
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
                weights=weights,
                targets=targets
            )

            return {
                "success": True,
                "verilator_results": v_json,
                "openlane_results": o_json,
                "weights": weights,
                "targets": targets,
                "final_score": score
            }

    except Exception as e:
        return {"success": False, "error_message": str(e)}
