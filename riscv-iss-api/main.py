# riscv-iss-api/main.py

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from pathlib import Path
import tempfile, subprocess, zipfile, shutil, json, asyncio, os, stat
import aiofiles

app = FastAPI(title="ChipForge RISC-V ISS API", version="1.0.0")

RESULTS_DIR = Path("/app/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


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
    """Find run.py inside the bundle (root or subdir like riscv-iss/run.py)."""
    for p in root.rglob("run.py"):
        if p.is_file():
            return p
    return None


def _make_binaries_executable(root: Path):
    """Ensure any binaries in bin/ directories are executable after zip extraction."""
    for bin_dir in root.rglob("bin"):
        if bin_dir.is_dir():
            for f in bin_dir.iterdir():
                if f.is_file():
                    f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


async def _run_subprocess(cmd, cwd, timeout=3600):
    """Run subprocess asynchronously."""
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


@app.post("/simulate_iss", response_model=EvalResponse)
async def simulate_iss(
    design_zip: UploadFile = File(..., description="Miner design (zip with .S file)"),
    iss_bundle: UploadFile = File(..., description="Evaluator's ISS bundle (run.py, scripts/, common/, bin/)"),
    submission_id: str = Form(None)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)

            # ---- stash uploads once (avoid double .read()) ----
            design_bytes = await design_zip.read()
            bundle_bytes = await iss_bundle.read()

            # ---- prepare dirs ----
            design_dir = tmp / "design"
            bundle_dir = tmp / "bundle"
            design_dir.mkdir(parents=True, exist_ok=True)
            bundle_dir.mkdir(parents=True, exist_ok=True)

            # ---- unpack inputs using async file writes ----
            dz = tmp / "design.zip"
            bz = tmp / "iss_bundle.zip"

            await asyncio.gather(
                _write_upload_to(dz, design_bytes),
                _write_upload_to(bz, bundle_bytes)
            )

            _unzip(dz, design_dir)
            _unzip(bz, bundle_dir)

            # ---- ensure binaries are executable after zip extraction ----
            _make_binaries_executable(bundle_dir)

            # ---- locate run.py inside bundle ----
            run_py = _find_run_py(bundle_dir)
            if not run_py:
                return EvalResponse(
                    success=False,
                    error_message="iss_bundle is missing run.py (looked recursively)."
                )

            # ---- invoke run.py asynchronously ----
            cmd = [
                "python3", str(run_py),
                "--design", str(design_dir),
                "--resources", str(bundle_dir)
            ]

            proc = await _run_subprocess(cmd, tmp, timeout=3600)

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
            results_zip_path = None
            details = payload.get("details", {})
            rz = details.get("results_zip")
            if rz:
                rz_path = Path(rz)
                if rz_path.exists():
                    final_zip = RESULTS_DIR / f"{submission_id}_riscv_iss.zip"
                    shutil.copy(rz_path, final_zip)
                    results_zip_path = str(final_zip)
                    details["results_zip"] = results_zip_path

            return EvalResponse(
                success=True,
                results=payload,
                results_zip_path=results_zip_path
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
    return {"status": "healthy", "version": "1.0.0"}
