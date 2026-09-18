# verilator-api/main.py

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from pathlib import Path
import os, tempfile, subprocess, zipfile, shutil, json, asyncio, aiofiles

app = FastAPI(title="ChipForge Verilator API", version="4.0.0")

RESULTS_DIR = Path("/app/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------------------------
# Capacity control.
#
# An evaluation is not one process: the bundle's run.py fans out into a batch of simulations, and
# it sizes that batch from the CPUs it can see. That is correct for ONE evaluation and wrong for
# several, because each concurrent request sizes itself as though it owned the machine. Measured
# on a 12-core host: four concurrent evaluations launched 36 simulations, everything crawled, and
# all four failed on their own internal budget.
#
# The service owns the host, so the service decides. Two levers, both sized at import:
#   * how many evaluations may run at once, and
#   * how wide each one may fan out -- handed to run.py through the knob it already reads.
# Their product is what actually lands on the CPUs, and it is what we keep under the core count.
def _cpus():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 2)


_CPUS = _cpus()
EVAL_LANES = int(os.environ.get("VERILATOR_EVAL_LANES", "0")) or max(1, min(4, _CPUS // 6))
FANOUT_PER_EVAL = int(os.environ.get("VERILATOR_FANOUT", "0")) or max(1, (_CPUS - 1) // EVAL_LANES)
_eval_semaphore = asyncio.Semaphore(EVAL_LANES)
print(f"[verilator-api] {_CPUS} cpus -> {EVAL_LANES} concurrent evaluations x {FANOUT_PER_EVAL} "
      f"jobs each ({EVAL_LANES * FANOUT_PER_EVAL} peak)", flush=True)

class EvalResponse(BaseModel):
    success: bool
    results: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    evaluator_log: Optional[str] = None
    results_zip_path: Optional[str] = None


async def _write_upload_to(path: Path, data: bytes):
    async with aiofiles.open(path, 'wb') as f:
        await f.write(data)


def _unzip(zippath: Path, dest: Path):
    with zipfile.ZipFile(zippath, "r") as zf:
        zf.extractall(dest)


def _find_run_py(root: Path) -> Optional[Path]:
    # accept run.py anywhere inside the bundle (root or subdir like verilator/run.py)
    for p in root.rglob("run.py"):
        if p.is_file():
            return p
    return None


async def _run_subprocess(cmd, cwd, timeout=3600, env=None):
    """Run subprocess asynchronously"""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return_code = process.returncode
        
        return {
            'returncode': return_code,
            'stdout': stdout.decode('utf-8') if stdout else '',
            'stderr': stderr.decode('utf-8') if stderr else ''
        }
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(cmd, timeout)


@app.post("/simulate_and_evaluate", response_model=EvalResponse)
async def simulate_and_evaluate(
    design_zip: UploadFile = File(..., description="Miner design (zip with rtl.f + rtl/...)"),
    verilator_bundle: UploadFile = File(..., description="Evaluator's Verilator bundle (run.py, tb_files.f, Makefile, Regression.mk, tests/, scripts/, top_module.txt)"),
    submission_id: str = Form(None)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)

            # ---- stash uploads once (avoid double .read()) ----
            design_bytes = await design_zip.read()
            bundle_bytes = await verilator_bundle.read()

            # ---- prepare dirs ----
            design_dir = tmp / "design"
            bundle_dir = tmp / "bundle"
            design_dir.mkdir(parents=True, exist_ok=True)
            bundle_dir.mkdir(parents=True, exist_ok=True)

            # ---- unpack inputs using async file writes ----
            dz = tmp / "design.zip"
            bz = tmp / "verilator_bundle.zip"
            
            # Write files asynchronously in parallel
            await asyncio.gather(
                _write_upload_to(dz, design_bytes),
                _write_upload_to(bz, bundle_bytes)
            )
            
            # Unzip operations (keep sync as they're fast)
            _unzip(dz, design_dir)
            _unzip(bz, bundle_dir)

            # ---- locate run.py inside bundle ----
            run_py = _find_run_py(bundle_dir)
            if not run_py:
                return EvalResponse(
                    success=False,
                    error_message="verilator_bundle is missing run.py (looked recursively)."
                )

            # ---- invoke run.py asynchronously ----
            cmd = [
                "python3", str(run_py),
                "--design", str(design_dir),
                "--resources", str(bundle_dir),
            ]
            
            # NPUV1_MAX_PARALLEL is the knob the bundle already honours; the service sets it so
            # the batch is sized against this host's share, not against the whole machine.
            env = dict(os.environ, NPUV1_MAX_PARALLEL=str(FANOUT_PER_EVAL))
            async with _eval_semaphore:
                proc = await _run_subprocess(cmd, tmp, timeout=3600, env=env)

            if proc['returncode'] != 0:
                return EvalResponse(
                    success=False,
                    error_message="run.py failed",
                    evaluator_log=proc['stdout'] + "\n" + proc['stderr']
                )

            # ---- parse run.py stdout as JSON ----
            try:
                payload = json.loads((proc['stdout'] or "").strip())
            except Exception as e:
                return EvalResponse(
                    success=False,
                    error_message=f"run.py did not return valid JSON: {e}",
                    evaluator_log=proc['stdout'] + "\n" + proc['stderr']
                )

            # ---- copy results.zip if run.py created it ----
            # results_zip_path = None
            # details = payload.get("details", {})
            # rz = details.get("results_zip")
            # if rz:
            #     rz_path = Path(rz)
            #     if rz_path.exists():
            #         final_zip = RESULTS_DIR / f"{submission_id}_verilator.zip"
            #         shutil.copy(rz_path, final_zip)
            #         results_zip_path = str(final_zip)
            #         # rewrite in payload for convenience
            #         details["results_zip"] = results_zip_path

            return EvalResponse(
                success=True,
                results=payload,
                results_zip_path=None
            )

    except Exception as e:
        return EvalResponse(success=False, error_message=str(e))


@app.get("/download_results")
async def download_results():
    z = RESULTS_DIR / "results.zip"
    if not z.exists():
        return {"error": "No results.zip found"}
    return FileResponse(z, filename="results.zip")


@app.get("/health")
async def health():
    return {"status": "healthy", "version": "4.1.0"}