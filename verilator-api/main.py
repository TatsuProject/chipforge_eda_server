from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
import tempfile
import subprocess
from pathlib import Path
import zipfile
import json
import shutil

app = FastAPI(title="Verilator API (Evaluator-driven)", version="3.4.0")

# Permanent place for results
RESULTS_DIR = Path("/app/results")
RESULTS_DIR.mkdir(exist_ok=True)

# -------------------------------
# Response Model
# -------------------------------
class EvalResponse(BaseModel):
    success: bool
    results: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    evaluator_log: Optional[str] = None
    results_zip_path: Optional[str] = None


# -------------------------------
# Simulate + Evaluate
# -------------------------------
@app.post("/simulate_and_evaluate", response_model=EvalResponse)
async def simulate_and_evaluate(
    design_zip: UploadFile = File(..., description="Miner design (zip with verilator.f + tb + rtl)"),
    top_module: str = Form(..., description="Top module name (testbench top)"),
    evaluator_py: UploadFile = File(..., description="Evaluator script (Evaluator.py)"),
    evaluator_zip: Optional[UploadFile] = File(None, description="Evaluator resources (Makefiles, tests/, scripts/)")
):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)

            # --- Save design.zip ---
            design_zip_path = workdir / "design.zip"
            with open(design_zip_path, "wb") as f:
                f.write(await design_zip.read())
            with zipfile.ZipFile(design_zip_path, "r") as zf:
                zf.extractall(workdir / "design")

            # --- Save Evaluator.py ---
            evaluator_path = workdir / "Evaluator.py"
            with open(evaluator_path, "wb") as f:
                f.write(await evaluator_py.read())

            # --- Save evaluator.zip if provided ---
            resources_dir = None
            if evaluator_zip:
                evaluator_zip_path = workdir / "evaluator.zip"
                with open(evaluator_zip_path, "wb") as f:
                    f.write(await evaluator_zip.read())
                resources_dir = workdir / "evaluator_resources"
                with zipfile.ZipFile(evaluator_zip_path, "r") as zf:
                    zf.extractall(resources_dir)

            # --- Run Evaluator.py ---
            cmd = [
                "python3", str(evaluator_path),
                "--design", str(workdir / "design"),
                "--top", top_module,
            ]
            if resources_dir:
                cmd += ["--resources", str(resources_dir)]

            result = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=900
            )

            if result.returncode != 0:
                return EvalResponse(
                    success=False,
                    error_message="Evaluator.py failed",
                    evaluator_log=result.stdout + "\n" + result.stderr,
                )

            # --- Parse JSON output ---
            try:
                eval_json = json.loads(result.stdout)
            except Exception:
                return EvalResponse(
                    success=False,
                    error_message="Evaluator.py did not return valid JSON",
                    evaluator_log=result.stdout + "\n" + result.stderr,
                )

            # --- If results.zip exists → copy to /app/results ---
            results_zip_path = None
            if "details" in eval_json and "results_zip" in eval_json["details"]:
                rz = Path(eval_json["details"]["results_zip"])
                if rz.exists():
                    final_zip = RESULTS_DIR / "results.zip"
                    shutil.copy(rz, final_zip)
                    results_zip_path = str(final_zip)
                    eval_json["details"]["results_zip"] = results_zip_path

            return EvalResponse(
                success=True,
                results=eval_json,
                results_zip_path=results_zip_path
            )

    except Exception as e:
        return EvalResponse(success=False, error_message=str(e))


# -------------------------------
# Download results.zip
# -------------------------------
@app.get("/download_results")
async def download_results():
    zip_path = RESULTS_DIR / "results.zip"
    if not zip_path.exists():
        return {"error": "No results.zip found"}
    return FileResponse(zip_path, filename="results.zip")


# -------------------------------
# Health
# -------------------------------
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "3.4.0"}
