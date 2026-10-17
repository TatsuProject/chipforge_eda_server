"""Pass-through routes for the v2 evaluator service (/v2/*).

The gateway never interprets a v2 request or response. It forwards the request body as a stream
(the incoming Content-Length is forwarded, because the service refuses chunked uploads) and returns
the upstream status, body and a small set of headers unchanged. Only `Location` is rewritten from
the service's /status/<id> to the public /v2/status/<id>.

Environment:
  V2_SERVICE_URL   base URL of the v2 service. Empty = v2 not configured (503 on every /v2 route).
  V2_ADMIN_TOKEN   if set, POST /v2/evaluator requires `Authorization: Bearer <token>`.
  V2_CONNECT_TIMEOUT_S (5), V2_READ_TIMEOUT_S (60, plain requests), V2_UPLOAD_TIMEOUT_S (900, uploads)
"""
import os
import re
import secrets

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter(prefix="/v2")

_ID = r"[A-Za-z0-9._-]{1,128}"
_FWD_REQ = ("content-type", "content-length", "x-request-id", "accept")
_FWD_RESP = ("content-type", "cache-control", "retry-after", "x-request-id",
             "x-cf-evaluator-sha256", "allow")


def _cfg():
    return (os.environ.get("V2_SERVICE_URL", "").strip().rstrip("/"),
            os.environ.get("V2_ADMIN_TOKEN", ""),
            float(os.environ.get("V2_CONNECT_TIMEOUT_S", "5")),
            float(os.environ.get("V2_READ_TIMEOUT_S", "60")),
            float(os.environ.get("V2_UPLOAD_TIMEOUT_S", "900")))


def _err(status, code, message, **extra):
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status,
                        headers={"Cache-Control": "no-store"})


async def _body(request: Request):
    async for chunk in request.stream():
        yield chunk


async def _forward(request: Request, method: str, upstream_path: str, upload=False):
    base, _, t_conn, t_read, t_up = _cfg()
    if not base:
        return _err(503, "V2_NOT_CONFIGURED", "The v2 evaluator is not enabled on this server.")
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FWD_REQ}
    headers.setdefault("X-Request-ID", secrets.token_hex(8))
    data = None
    if upload:
        if "content-length" not in request.headers:
            return _err(411, "CONTENT_LENGTH_REQUIRED", "Provide one Content-Length (no chunked upload).")
        data = _body(request)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=t_conn, sock_read=t_up if upload else t_read)
    try:
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as s:
            async with s.request(method, base + upstream_path, headers=headers, data=data,
                                 allow_redirects=False) as r:
                body = await r.read()
                out = {k: v for k, v in r.headers.items() if k.lower() in _FWD_RESP}
                loc = r.headers.get("Location")
                if loc:
                    out["Location"] = re.sub(r"^/status/", "/v2/status/", loc)
                return Response(content=body, status_code=r.status, headers=out)
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        return _err(503, "V2_UNREACHABLE", "The v2 evaluator service is not reachable.",
                    detail=type(e).__name__, retryable=True)
    except Exception as e:  # upstream closed mid-upload etc.
        if isinstance(e, (RuntimeError,)) and "disconnect" in str(e).lower():
            return _err(400, "CLIENT_DISCONNECTED", "Upload interrupted.")
        raise


def _check_admin(request: Request):
    tok = os.environ.get("V2_ADMIN_TOKEN", "")
    if tok and not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + tok):
        return _err(401, "ADMIN_TOKEN_REQUIRED", "Installing an evaluator requires the admin token.")
    return None


@router.post("/evaluator")
async def install_evaluator(request: Request):
    return _check_admin(request) or await _forward(request, "POST", "/evaluator", upload=True)


@router.get("/evaluator")
async def list_evaluators(request: Request):
    return await _forward(request, "GET", "/evaluator")


@router.get("/evaluator/{sha}")
async def get_evaluator(sha: str, request: Request):
    if not re.fullmatch(_ID, sha):
        return _err(404, "NOT_FOUND", "Unknown evaluator.")
    return await _forward(request, "GET", "/evaluator/" + sha)


@router.post("/evaluate")
async def evaluate(request: Request):
    return await _forward(request, "POST", "/evaluate", upload=True)


@router.get("/status/{request_id}")
async def status(request_id: str, request: Request):
    if not re.fullmatch(_ID, request_id):
        return _err(404, "NOT_FOUND", "Unknown request id.")
    return await _forward(request, "GET", "/status/" + request_id)


@router.get("/health")
async def health(request: Request):
    return await _forward(request, "GET", "/health")
