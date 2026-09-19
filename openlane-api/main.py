# openlane-api/main.py

import os, json, zipfile, tempfile, shutil, subprocess, asyncio, aiofiles, uuid
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ChipForge Openlane API", version="4.0.0")

# How many synthesis runs may proceed at once.
#
# This was a hard 1. The collision it was guarding against had already been fixed in the same
# change that introduced it -- each request now stages into its own uuid-named directory, so
# concurrent runs no longer share a path -- and serialising on top of that made synthesis the
# throughput ceiling for the whole service: N concurrent evaluations queue N synthesis runs end to
# end, and synthesis is the long pole of an evaluation.
#
# What genuinely does bound it is MEMORY, not correctness. A synthesis of a real accelerator peaks
# around 5 GB, so the limit is how many of those the host can hold at once; exceeding it gets a run
# OOM-killed, which looks like a submission failure and is not one. Sized from the host at import,
# overridable for a box whose memory profile differs.
MEM_PER_RUN_MB = int(os.environ.get("OPENLANE_MEM_PER_RUN_MB", "6144"))   # 5 GB peak + headroom


def _synthesis_lanes():
    budget_mb = MEM_PER_RUN_MB
    avail = None
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
                break
    except OSError:
        pass
    for lim_p, use_p in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                         ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
                          "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        try:
            lim = int(open(lim_p).read().split()[0]); use = int(open(use_p).read().split()[0])
            if 0 < lim < (1 << 62):                       # an "unlimited" cgroup reports a sentinel
                free = (lim - use) // (1024 * 1024)
                avail = free if avail is None else min(avail, free)
        except (OSError, ValueError):
            pass
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 2
    # Keep a quarter of what is free in reserve. Filling memory exactly is how a host with four
    # lanes x 6 GB on 28 GB ends up with nothing left for the page cache, the other service, or a
    # run that peaks slightly above its ceiling.
    by_mem = max(1, int(avail * 0.75) // budget_mb) if avail else 1
    return max(1, min(4, by_mem, cpus))


OPENLANE_LANES = int(os.environ.get("OPENLANE_LANES", "0")) or _synthesis_lanes()
_openlane_semaphore = asyncio.Semaphore(OPENLANE_LANES)
print(f"[openlane-api] {OPENLANE_LANES} concurrent synthesis run(s) x {MEM_PER_RUN_MB} MB each",
      flush=True)
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

@app.post("/run_openlane", response_model=RunResponse)
async def run_openlane(
    design_zip: UploadFile = File(..., description="Miner design.zip (rtl/, rtl.f, etc.)"),
    openlane_bundle: UploadFile = File(..., description="Bundle with flow.tcl, config.json, constraints.sdc, run.py"),
    submission_id: str = Form(None)
):
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            work = Path(tmpd)
            # Unique name so concurrent requests don't collide in /openlane/designs/
            design_dir = work / f"design_{uuid.uuid4().hex[:8]}"
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

            # --- Execute run.py asynchronously (serialized via semaphore) ---
            cmd = [
                "python3", str(run_py),
                "--design", str(design_dir),
                "--out", str(out_dir),
            ]

            openlane_design_copy = Path("/openlane/designs") / design_dir.name
            # Reserving a lane is only half of it: the reservation has to be enforced inside the
            # run, or a design that grows without bound exhausts the host and the OOM killer picks
            # its victim by footprint rather than by blame -- letting one submission fail another.
            env = dict(os.environ, NPUV1_SYNTH_MEM_MB=str(MEM_PER_RUN_MB))
            async with _openlane_semaphore:
                run = await _run_subprocess(cmd, work, timeout=3600, env=env)
                # Clean up the design copy written into /openlane/designs/ by run.py
                if openlane_design_copy.exists():
                    shutil.rmtree(openlane_design_copy, ignore_errors=True)

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
            # out_zip = work / "results.zip"
            # _safe_zip_dir(out_dir, out_zip)
            # final_zip = RESULTS_DIR / f"{submission_id}_openlane.zip"
            # shutil.copy(out_zip, final_zip)

            return RunResponse(
                success=True,
                results=result_obj,
                results_zip_path=None,
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