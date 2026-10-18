"""ChipForge EDA gateway, npu-v2 only.

Routes:
  GET  /health   liveness of the gateway itself (no evaluation is run)
  /v2/*          pass-through to the v2 evaluator service (v2_proxy.py)
  /evaluate      the retired npu-v1 route: 410 Gone, with a pointer to /v2/evaluate

npu-v1 has ended; its simulation and synthesis services are no longer part of this server.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from v2_proxy import router as _v2_router

app = FastAPI(title="ChipForge EDA Tools Gateway", version="6.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)  # no free schema for a scanner
app.include_router(_v2_router)   # /v2/* pass-through; 503 unless V2_SERVICE_URL is set


@app.get("/health")
async def health():
    """Liveness for validators' pre-flight check (no evaluation is run). /v2/health is the evaluator's."""
    return {"status": "ok"}


V1_GONE = {
    "success": False,
    "error": {
        "code": "V1_RETIRED", "category": "client", "fault": "client", "retryable": False,
        "stage": "gateway",
        "message": "npu-v1 has ended and this server no longer evaluates v1 submissions. "
                   "Use POST /v2/evaluate (see GET /v2/health).",
    },
}


@app.api_route("/evaluate", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def evaluate_v1_retired(request: Request):
    """The v1 route. The request body is never read."""
    return JSONResponse(V1_GONE, status_code=410, headers={"Cache-Control": "no-store"})
