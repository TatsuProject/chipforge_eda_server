# openlane-api/main.py

import json, zipfile, tempfile, shutil, subprocess, asyncio, aiofiles
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ChipForge Openlane API", version="4.0.0")
RESULTS_DIR = Path("/app/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

class RunResponse(BaseModel):
    success: bool
    results: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    logs: Optional[str] = None
    results_zip_path: Optional[str] = None

def _read_text(p: Path) -> str:
    try:
        return p.read_text(errors="ignore")
    except Exception:
        return ""

def _safe_zip_dir(src_dir: Path, out_zip: Path):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(src_dir)))

def _find_run_py(bundle_dir: Path) -> Optional[Path]:
    for p in bundle_dir.rglob("run.py"):
        if p.is_file():
            return p
    return None

async def _run_subprocess(cmd, cwd, timeout=3600):
    """Run subprocess asynchronously"""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
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

@app.post("/run_openlane", response_model=RunResponse)
async def run_openlane(
    design_zip: UploadFile = File(..., description="Miner design.zip (rtl/, rtl.f, etc.)"),
    openlane_bundle: UploadFile = File(..., description="Bundle with flow.tcl, config.json, constraints.sdc, run.py"),
    submission_id: str = Form(None)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            work = Path(tmpd)
            design_dir = work / "design"
            bundle_dir = work / "bundle"
            out_dir    = work / "out"
            for d in (design_dir, bundle_dir, out_dir):
                d.mkdir(parents=True, exist_ok=True)

            # --- Save & extract design.zip and bundle asynchronously ---
            dz = work / "design.zip"
            bz = work / "openlane_bundle.zip"
            
            # Read and write files in parallel
            design_bytes = await design_zip.read()
            bundle_bytes = await openlane_bundle.read()
            
            async with aiofiles.open(dz, 'wb') as f:
                await f.write(design_bytes)
            async with aiofiles.open(bz, 'wb') as f:
                await f.write(bundle_bytes)
            
            # Extract zips (keep sync - zipfile operations are fast)
            with zipfile.ZipFile(dz, "r") as zf:
                zf.extractall(design_dir)
            with zipfile.ZipFile(bz, "r") as zf:
                zf.extractall(bundle_dir)

            # --- Locate run.py inside bundle ---
            run_py = _find_run_py(bundle_dir)
            if not run_py:
                return RunResponse(success=False, error_message="run.py missing from bundle")

            # --- Execute run.py asynchronously ---
            cmd = [
                "python3", str(run_py),
                "--design", str(design_dir),
                "--out", str(out_dir),
            ]
            
            run = await _run_subprocess(cmd, work, timeout=3600)

            if run['returncode'] != 0:
                return RunResponse(
                    success=False,
                    error_message="run.py failed",
                    logs=run['stdout'] + "\n" + run['stderr']
                )

            # --- Parse stdout JSON ---
            try:
                result_obj = json.loads(run['stdout'].strip())
            except Exception:
                res_json = out_dir / "results.json"
                if res_json.exists():
                    result_obj = json.loads(_read_text(res_json))
                else:
                    return RunResponse(
                        success=False,
                        error_message="run.py did not return valid JSON",
                        logs=run['stdout'] + "\n" + run['stderr']
                    )

            # --- Zip results folder ---
            out_zip = work / "results.zip"
            _safe_zip_dir(out_dir, out_zip)
            final_zip = RESULTS_DIR / f"{submission_id}_openlane.zip"
            shutil.copy(out_zip, final_zip)

            return RunResponse(
                success=True,
                results=result_obj,
                results_zip_path=str(final_zip),
                logs=run['stderr']
            )

    except Exception as e:
        return RunResponse(success=False, error_message=str(e))

@app.get("/download_results")
async def download_results():
    z = RESULTS_DIR / "results.zip"
    if not z.exists():
        return {"error": "No results.zip found"}
    return FileResponse(z, filename="results.zip")

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "3.0.1"}