"""ChipForge EDA gateway, npu-v2 only.

Routes:
  GET  /health   liveness of the gateway itself (no evaluation is run)
  /v2/*          pass-through to the v2 evaluator service (v2_proxy.py); with EDA_MOCK_FILE set, answered by
                 the mock instead (v2_mock.py, testing only)
  /v2/jobs, /v2/cancel/<id>   unfinished evaluations; cancel one (jobs.py: abandoned ones are cancelled automatically)
  /evaluate      the retired npu-v1 route: 410 Gone, with a pointer to /v2/evaluate

npu-v1 has ended; its simulation and synthesis services are no longer part of this server.
"""
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import housekeeping
import jobs
from v2_proxy import active_evaluator, router as _v2_router
import v2_mock

app = FastAPI(title="ChipForge EDA Tools Gateway", version="6.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)  # no free schema for a scanner
if v2_mock.enabled():             # TESTING ONLY: EDA_MOCK_FILE answers /v2/* itself (no evaluator needed)
    print(f"[MOCK] v2 mock mode ON ({v2_mock.EDA_MOCK_FILE}): no EDA tools run; never use this to evaluate "
          f"real submissions", flush=True)
    app.include_router(v2_mock.router)
else:
    app.include_router(_v2_router)   # /v2/* pass-through; 503 unless V2_SERVICE_URL is set
app.include_router(jobs.router)       # /v2/jobs, /v2/cancel/<id>: abandoned evaluations are cancelled (jobs.py)


@app.get("/health")
async def health():
    """Liveness for validators' pre-flight check (no evaluation is run). /v2/health is the evaluator's."""
    return {"status": "ok", "eda_version": housekeeping.VERSION}


async def _cleanup_loop():
    """Old evaluator versions are deleted every CF_V2_PRUNE_SECONDS (housekeeping.py)."""
    await asyncio.sleep(60)                          # let the evaluator start
    while True:
        try:
            await housekeeping.prune("periodic", await active_evaluator())
        except Exception as e:                       # never let housekeeping take the gateway down
            print(f"[cleanup] failed: {type(e).__name__}", flush=True)
        await asyncio.sleep(housekeeping.prune_seconds())


@app.on_event("startup")
async def _start_cleanup():
    print(f"[gateway] EDA server {housekeeping.VERSION}", flush=True)
    if not v2_mock.enabled():
        app.state.cleanup = asyncio.create_task(_cleanup_loop())
    # from the start: evaluations an older version left running for nobody are cancelled (jobs.py)
    app.state.watchdog = asyncio.create_task(jobs.loop())


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
