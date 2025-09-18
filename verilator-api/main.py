from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from pathlib import Path
import tempfile, subprocess, zipfile, shutil, json

app = FastAPI(title="ChipForge Verilator API", version="4.0.0")

RESULTS_DIR = Path("/app/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

class EvalResponse(BaseModel):
    success: bool
    results: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    evaluator_log: Optional[str] = None
    results_zip_path: Optional[str] = None


def _write_upload_to(path: Path, data: bytes):
    path.write_bytes(data)


def _unzip(zippath: Path, dest: Path):
    with zipfile.ZipFile(zippath, "r") as zf:
        zf.extractall(dest)


def _find_run_py(root: Path) -> Optional[Path]:
    # accept run.py anywhere inside the bundle (root or subdir like verilator/run.py)
    for p in root.rglob("run.py"):
        if p.is_file():
            return p
    return None


@app.post("/simulate_and_evaluate", response_model=EvalResponse)
async def simulate_and_evaluate(
    design_zip: UploadFile = File(..., description="Miner design (zip with rtl.f + rtl/...)"),
    verilator_bundle: UploadFile = File(..., description="Evaluator's Verilator bundle (run.py, tb_files.f, Makefile, Regression.mk, tests/, scripts/, top_module.txt)")
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

            # ---- unpack inputs ----
            dz = tmp / "design.zip"
            bz = tmp / "verilator_bundle.zip"
            _write_upload_to(dz, design_bytes)
            _write_upload_to(bz, bundle_bytes)
            _unzip(dz, design_dir)
            _unzip(bz, bundle_dir)

            # ---- locate run.py inside bundle ----
            run_py = _find_run_py(bundle_dir)
            if not run_py:
                return EvalResponse(
                    success=False,
                    error_message="verilator_bundle is missing run.py (looked recursively)."
                )

            # ---- invoke run.py ----
            # run.py is responsible for copying/merging resources into design_dir,
            # creating verilator.f, running make/Regression, and emitting JSON on stdout.
            cmd = [
                "python3", str(run_py),
                "--design", str(design_dir),
                "--resources", str(bundle_dir)
            ]
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=3600)

            if proc.returncode != 0:
                return EvalResponse(
                    success=False,
                    error_message="run.py failed",
                    evaluator_log=(proc.stdout or "") + "\n" + (proc.stderr or "")
                )

            # ---- parse run.py stdout as JSON ----
            try:
                payload = json.loads((proc.stdout or "").strip())
            except Exception as e:
                return EvalResponse(
                    success=False,
                    error_message=f"run.py did not return valid JSON: {e}",
                    evaluator_log=(proc.stdout or "") + "\n" + (proc.stderr or "")
                )

            # ---- copy results.zip if run.py created it ----
            results_zip_path = None
            details = payload.get("details", {})
            rz = details.get("results_zip")
            if rz:
                rz_path = Path(rz)
                if rz_path.exists():
                    final_zip = RESULTS_DIR / "results.zip"
                    shutil.copy(rz_path, final_zip)
                    results_zip_path = str(final_zip)
                    # rewrite in payload for convenience
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
    return {"status": "healthy", "version": "4.1.0"}
