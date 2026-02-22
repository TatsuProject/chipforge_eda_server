import os, zipfile, tempfile, json, aiohttp, asyncio, aiofiles
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import secrets
app = FastAPI(title="ChipForge EDA Tools Gateway", version="6.0.0")

# Service URLs (configurable via env vars, defaults match docker-compose)
VERILATOR_API  = os.getenv("VERILATOR_URL", "http://verilator-api:8001") + "/simulate_and_evaluate"
RISCV_ISS_API  = os.getenv("RISCV_ISS_URL", "http://riscv-iss-api:8002") + "/simulate_iss"
OPENLANE_API   = os.getenv("OPENLANE_URL", "http://openlane-api:8003") + "/run_openlane"

# Map service name -> (evaluator subdir, API URL, bundle form-field name)
SERVICE_CONFIG = {
    "verilator": ("verilator", VERILATOR_API, "verilator_bundle"),
    "riscv-iss": ("riscv-iss", RISCV_ISS_API, "iss_bundle"),
    "openlane":  ("openlane",  OPENLANE_API,  "openlane_bundle"),
}


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
    """Generate a unique submission ID."""
    id_length_bytes = length // 2
    submission_id = secrets.token_hex(id_length_bytes)
    return submission_id


def compute_weighted_score(func_0_1, area_um2, ips, power_mw, weights, targets):
    """Compute weighted score with functionality, area, performance, and power."""
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
    func_component = func_0_1
    area_component = min(area_ref / area_um2, ratio_cap) if area_um2 and area_ref > 0 else 0.0
    perf_component = min(ips / perf_ref, ratio_cap) if ips and perf_ref > 0 else 0.0
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

    # --- Gates ---
    func_gate    = func_0_1 >= func_threshold
    overall_gate = overall >= overall_threshold

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
    """Make async HTTP request with multipart files."""
    data = aiohttp.FormData()
    opened_files = []
    try:
        for field_name, value in files_data.items():
            if field_name == "submission_id":
                data.add_field(field_name, value)
            else:
                fh = open(value, 'rb')
                opened_files.append(fh)
                data.add_field(field_name, fh, filename=value.name)

        async with session.post(url, data=data) as response:
            return await response.json()
    finally:
        for fh in opened_files:
            fh.close()


def _infer_services(weights):
    """Backward compatibility: infer services list from weights when not explicit."""
    services = ["verilator"]
    if weights.get("area", 0) > 0 or weights.get("performance", 0) > 0:
        services.append("openlane")
    return services


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

            # extract evaluator
            eval_dir = work / "evaluator"
            eval_dir.mkdir()
            try:
                _unzip(eval_path, eval_dir)
            except zipfile.BadZipFile:
                return JSONResponse(
                    {"success": False, "error_message": "evaluator_zip is not a valid zip file."},
                    status_code=400
                )

            gateway_dir = eval_dir / "gateway"

            # ---- read config (prefer config.json, fall back to weights.json) ----
            weights, targets, services = {}, {}, None
            config_file = gateway_dir / "config.json"
            if not config_file.exists():
                config_file = gateway_dir / "weights.json"
            if config_file.exists():
                cfg = json.loads(config_file.read_text())
                weights = cfg.get("weights", {})
                targets = cfg.get("targets", {})
                services = cfg.get("services")  # explicit list or None

            # Backward compat: infer from weights if no explicit services list
            if services is None:
                services = _infer_services(weights)

            if not services:
                return JSONResponse(
                    {"success": False, "error_message": "No services configured in config.json"},
                    status_code=400
                )

            # ---- validate that required service directories exist ----
            missing = []
            for svc in services:
                cfg_entry = SERVICE_CONFIG.get(svc)
                if cfg_entry is None:
                    missing.append(f"Unknown service: {svc}")
                    continue
                subdir = eval_dir / cfg_entry[0]
                if not subdir.exists():
                    missing.append(f"{cfg_entry[0]}/")
            if missing:
                return JSONResponse(
                    {"success": False, "error_message": f"Missing in evaluator.zip: {', '.join(missing)}"},
                    status_code=400
                )

            # ---- build bundles for each service ----
            bundles = {}
            for svc in services:
                subdir_name, api_url, bundle_field = SERVICE_CONFIG[svc]
                src_dir = eval_dir / subdir_name
                bundle_zip = work / f"{subdir_name}_bundle.zip"
                _rezip(src_dir, bundle_zip)
                bundles[svc] = {
                    "api_url": api_url,
                    "bundle_field": bundle_field,
                    "bundle_zip": bundle_zip,
                }

            # ---- dispatch to services in parallel ----
            timeout = aiohttp.ClientTimeout(total=2700)  # 45 minutes
            async with aiohttp.ClientSession(timeout=timeout) as session:
                tasks = {}
                for svc, info in bundles.items():
                    files_data = {
                        "design_zip": design_path,
                        info["bundle_field"]: info["bundle_zip"],
                        "submission_id": submission_id,
                    }
                    tasks[svc] = make_http_request(session, info["api_url"], files_data)

                # Execute all in parallel
                task_keys = list(tasks.keys())
                task_coros = [tasks[k] for k in task_keys]
                raw_results = await asyncio.gather(*task_coros, return_exceptions=True)

                svc_results = {}
                for key, result in zip(task_keys, raw_results):
                    if isinstance(result, Exception):
                        err_str = str(result)
                        if "Cannot connect" in err_str or "Connection refused" in err_str:
                            err_str = f"Backend service '{key}' is not reachable. It may be down or still starting up."
                        elif "TimeoutError" in type(result).__name__:
                            err_str = f"Backend service '{key}' timed out after 45 minutes."
                        svc_results[key] = {"success": False, "error_message": err_str}
                    else:
                        svc_results[key] = result

            # ---- extract scores from service results ----
            func_score = 0.0
            ipc = None

            # functionality_score comes from verilator OR riscv-iss (whichever is present)
            for svc in ("verilator", "riscv-iss"):
                if svc in svc_results and svc_results[svc].get("success"):
                    res = svc_results[svc].get("results", {})
                    func_score = res.get("functionality_score", 0.0)
                    ipc = res.get("details", {}).get("ipc")
                    break  # take from first available

            # area/fmax/power come from openlane
            area_um2, fmax_mhz, power_mw = None, None, None
            if "openlane" in svc_results and svc_results["openlane"].get("success"):
                o_res = svc_results["openlane"].get("results", {})
                area_um2 = o_res.get("area_um2")
                fmax_mhz = o_res.get("fmax_mhz")
                power_mw = o_res.get("power_mw")

            # ---- compute IPS ----
            ips = None
            if ipc and fmax_mhz:
                ips = ipc * (fmax_mhz * 1e6)

            # ---- final score ----
            score = compute_weighted_score(
                func_0_1=func_score,
                area_um2=area_um2,
                ips=ips,
                power_mw=power_mw,
                weights=weights,
                targets=targets
            )

            # ---- collect errors from failed services ----
            service_errors = []
            for svc_name, svc_res in svc_results.items():
                if not svc_res.get("success"):
                    err = svc_res.get("error_message") or svc_res.get("error") or "unknown error"
                    service_errors.append(f"{svc_name}: {err}")

            # ---- build response ----
            all_succeeded = all(
                svc_res.get("success", False) for svc_res in svc_results.values()
            )
            response = {
                "success": all_succeeded or func_score > 0,
                "submission_id": submission_id,
                "weights": weights,
                "targets": targets,
                "final_score": score
            }

            if service_errors:
                response["service_errors"] = service_errors

            # Include per-service results with backward-compatible field names
            if "verilator" in svc_results:
                response["verilator_results"] = svc_results["verilator"]
            else:
                response["verilator_results"] = {"skipped": True}

            if "riscv-iss" in svc_results:
                response["riscv_iss_results"] = svc_results["riscv-iss"]

            if "openlane" in svc_results:
                response["openlane_results"] = svc_results["openlane"]
            else:
                response["openlane_results"] = {"skipped": True}

            return response

    except zipfile.BadZipFile:
        return {
            "success": False,
            "submission_id": submission_id if 'submission_id' in locals() else "",
            "error_message": "One of the uploaded files is not a valid zip archive."
        }
    except Exception as e:
        return {
            "success": False,
            "submission_id": submission_id if 'submission_id' in locals() else "",
            "error_message": f"Internal gateway error: {str(e)}"
        }
