from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, Dict, Any
import httpx
import os
from dotenv import load_dotenv
import logging
from pathlib import Path
from datetime import datetime
import traceback

# -------------------------------
# Load env
# -------------------------------
load_dotenv()
app = FastAPI(title="EDA Tools Gateway", version="3.3.3")

# Security
security = HTTPBearer()
API_KEY = os.getenv("EDA_API_KEY")

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API key not configured")
    if credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials

# Backend service URLs
YOSYS_URL     = os.getenv("YOSYS_URL", "http://yosys-api:8000")
VERILATOR_URL = os.getenv("VERILATOR_URL", "http://verilator-api:8001")
ICARUS_URL    = os.getenv("ICARUS_URL", "http://icarus-api:8002")
OPENLANE_URL  = os.getenv("OPENLANE_URL", "http://openlane-api:8003")

# AWS Configuration (optional)
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_BUCKET_NAME       = os.getenv("AWS_BUCKET_NAME")
AWS_REGION            = os.getenv("AWS_REGION", "us-east-1")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------
# Response Model
# -------------------------------
class VerilogEvaluationResponse(BaseModel):
    success: bool
    functionality_score: Optional[float] = 0.0
    area_score: Optional[float] = 0.0
    delay_score: Optional[float] = 0.0
    power_score: Optional[float] = 0.0
    overall_score: Optional[float] = 0.0
    detailed_results: Dict[str, Any]
    results_zip_path: Optional[str] = None
    error_message: Optional[str] = None


# -------------------------------
# Evaluate endpoint
# -------------------------------
@app.post("/evaluate", response_model=VerilogEvaluationResponse)
async def evaluate_verilog_design(
    design_zip: UploadFile = File(..., description="Zip containing miner sources + verilator.f"),
    api_key: str = Depends(verify_api_key)
):
    """
    Evaluate design using local evaluator files.
    """
    results: Dict[str, Any] = {}
    try:
        # Load evaluator files from server's local directory
        evaluator_py_path = "/app/evaluator/evaluator.py"
        evaluator_zip_path = "/app/evaluator/evaluator.zip"
        top_module_path = "/app/evaluator/evaluator.txt"
        
        # Read top module name from file
        with open(top_module_path, 'r') as f:
            top_module = f.read().strip()

        # top_module="tb_top"
        
        async with httpx.AsyncClient(timeout=900.0) as client:
            files = {
                "design_zip": (design_zip.filename, await design_zip.read(), "application/zip"),
                "evaluator_py": ("miniRISC_evaluator.py", open(evaluator_py_path, 'rb').read(), "application/x-python"),
                "evaluator_zip": ("miniRISC_evaluator.zip", open(evaluator_zip_path, 'rb').read(), "application/zip"),
            }
            data = {"top_module": top_module}

            v_resp = await client.post(
                f"{VERILATOR_URL}/simulate_and_evaluate",
                files=files,
                data=data
            )

            if v_resp.status_code != 200:
                return VerilogEvaluationResponse(
                    success=False,
                    detailed_results={"verilator_error": v_resp.text},
                    error_message=f"Verilator API error {v_resp.status_code}: {v_resp.text}"
                )

            v_json = v_resp.json()
            results["verilator_simulation"] = v_json
            logger.info(f"[Gateway] Verilator success={v_json.get('success')}")

            if not v_json.get("success"):
                return VerilogEvaluationResponse(
                    success=False,
                    detailed_results=results,
                    error_message=f"Verilator failed: {v_json.get('error_message','')}\n\n"
                                  f"LOG:\n{v_json.get('evaluator_log','')}"
                )

            # Extract functionality score
            func_score = v_json.get("results", {}).get("functionality_score")

            # Save results.zip if present
            results_zip_path = None
            if v_json.get("results_zip_path"):
                try:
                    # Instead of local path, we assume Verilator API exposes it at /download_results
                    zip_url = f"{VERILATOR_URL}/download_results"
                    zip_filename = Path("results") / f"verilator_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
                    Path("results").mkdir(exist_ok=True)

                    r = await client.get(zip_url)
                    if r.status_code == 200:
                        with open(zip_filename, "wb") as f:
                            f.write(r.content)
                        results_zip_path = str(zip_filename)

                        # Optional: Upload to AWS
                        if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_BUCKET_NAME:
                            try:
                                import boto3
                                s3_client = boto3.client(
                                    "s3",
                                    aws_access_key_id=AWS_ACCESS_KEY_ID,
                                    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                                    region_name=AWS_REGION,
                                )
                                s3_key = f"eda_results/{zip_filename.name}"
                                s3_client.upload_file(str(zip_filename), AWS_BUCKET_NAME, s3_key)
                                results_zip_path = f"s3://{AWS_BUCKET_NAME}/{s3_key}"
                                logger.info(f"Results uploaded to AWS: {results_zip_path}")
                            except Exception as e:
                                logger.warning(f"AWS upload failed: {e}")
                except Exception as e:
                    logger.warning(f"Could not download results.zip: {e}")

            return VerilogEvaluationResponse(
                success=True,
                functionality_score=func_score*100.0,
                area_score=0.0,
                delay_score=0.0,
                power_score=0.0,
                overall_score=func_score*100.0,
                detailed_results=results,
                results_zip_path=results_zip_path,
                error_message=None
            )

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Evaluation failed: {e}\n{tb}")
        return VerilogEvaluationResponse(
            success=False,
            detailed_results=results,
            results_zip_path=None,
            error_message=f"Evaluation failed: {str(e)}"
        )


# -------------------------------
# Health
# -------------------------------
@app.get("/health")
async def health_check(api_key: str = Depends(verify_api_key)):
    """Check status of gateway + backend services"""
    status = {"gateway": "healthy", "services": {}}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, url in {
                "yosys": YOSYS_URL,
                "verilator": VERILATOR_URL,
                "icarus": ICARUS_URL,
                "openlane": OPENLANE_URL,
            }.items():
                try:
                    resp = await client.get(f"{url}/health")
                    status["services"][name] = resp.json() if resp.status_code == 200 else "unhealthy"
                except Exception:
                    status["services"][name] = "unreachable"
    except Exception as e:
        status["gateway"] = f"error: {e}"

    return status
