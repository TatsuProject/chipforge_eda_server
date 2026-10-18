"""Gateway /v2 proxy against a fake upstream (local HTTP server). Run: pytest gateway/tests"""
import hashlib, json, os, sys, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main  # noqa: E402

SEEN = []
RAW = b'{"b": 1,  "a":[1, 2],\n"u":"\xc3\xa9"}\n'   # deliberately odd formatting: must survive byte-for-byte


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra: self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        SEEN.append(("GET", self.path, None))
        p = self.path
        if p == "/status/done": self._send(200, RAW, [("X-CF-Evaluator-Sha256", "ab")])
        elif p == "/status/busy": self._send(202, b'{"state":"queued"}', [("Retry-After", "30")])
        elif p == "/health": self._send(503, b'{"ready":false}')
        elif p.startswith("/evaluator/"): self._send(200, b'{"phase":"ready"}')
        else: self._send(404, b'{"error":"NOT_FOUND"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length", -1))
        if n < 0 or self.headers.get("Transfer-Encoding"):
            return self._send(411, b'{"error":"CONTENT_LENGTH_REQUIRED"}')
        body = self.rfile.read(n)
        SEEN.append(("POST", self.path, hashlib.sha256(body).hexdigest(), n, self.headers.get("Content-Type")))
        if self.path == "/evaluate":
            if b"mismatch" in body[:4096]: return self._send(409, b'{"error":"EVALUATOR_MISMATCH"}')
            return self._send(202, b'{"request_id":"r1"}', [("Location", "/status/r1"), ("X-Request-ID", "r1")])
        if self.path == "/evaluator": return self._send(202, b'{"install_url":"/evaluator/x"}')
        self._send(404, b"{}")


@pytest.fixture(scope="module")
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def client(upstream, monkeypatch):
    monkeypatch.setenv("V2_SERVICE_URL", upstream)
    monkeypatch.delenv("V2_ADMIN_TOKEN", raising=False)
    SEEN.clear()
    return TestClient(main.app)


def test_status_passthrough_bytes_and_headers(client):
    r = client.get("/v2/status/done")
    assert r.status_code == 200 and r.content == RAW
    assert r.headers["content-length"] == str(len(RAW))
    assert r.headers["x-cf-evaluator-sha256"] == "ab"


def test_status_202_and_404(client):
    r = client.get("/v2/status/busy")
    assert r.status_code == 202 and r.headers["retry-after"] == "30"
    r = client.get("/v2/status/nope")
    assert r.status_code == 404 and r.json()["error"] == "NOT_FOUND"


def test_evaluate_202_location_rewritten_and_409(client):
    r = client.post("/v2/evaluate", files={"design_zip": ("d.zip", b"x" * 100)}, data={"request_id": "r1"})
    assert r.status_code == 202 and r.content == b'{"request_id":"r1"}'
    assert r.headers["location"] == "/v2/status/r1"
    r = client.post("/v2/evaluate", data={"request_id": "mismatch"})
    assert r.status_code == 409 and r.json()["error"] == "EVALUATOR_MISMATCH"


def test_large_upload_identical_with_content_length(client):
    blob = os.urandom(40 * 1024 * 1024)
    r = client.post("/v2/evaluator", files={"evaluator_zip": ("e.zip", blob)})
    assert r.status_code == 202
    m, path, digest, n, ctype = SEEN[-1]
    assert path == "/evaluator" and ctype.startswith("multipart/form-data")
    assert n > len(blob)
    r = client.post("/v2/evaluator", content=blob, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 202
    assert SEEN[-1][2] == hashlib.sha256(blob).hexdigest() and SEEN[-1][3] == len(blob)


def test_evaluator_get_and_health(client):
    assert client.get("/v2/evaluator/abc123").json() == {"phase": "ready"}
    r = client.get("/v2/health")
    assert r.status_code == 503 and r.content == b'{"ready":false}'


def test_bad_ids_not_forwarded(client):
    assert client.get("/v2/status/..%2f..%2fx").status_code == 404
    assert not SEEN


def test_upstream_down_503(monkeypatch):
    monkeypatch.setenv("V2_SERVICE_URL", "http://127.0.0.1:1")
    c = TestClient(main.app)
    for r in (c.get("/v2/health"), c.post("/v2/evaluate", data={"a": "b"})):
        assert r.status_code == 503 and r.json()["error"] == "V2_UNREACHABLE"


def test_not_configured_503(monkeypatch):
    monkeypatch.setenv("V2_SERVICE_URL", "")
    r = TestClient(main.app).get("/v2/status/x")
    assert r.status_code == 503 and r.json()["error"] == "V2_NOT_CONFIGURED"


def test_admin_token(client, monkeypatch):
    monkeypatch.setenv("V2_ADMIN_TOKEN", "s3")
    assert client.post("/v2/evaluator", data={"a": "b"}).status_code == 401
    r = client.post("/v2/evaluator", data={"a": "b"}, headers={"Authorization": "Bearer s3"})
    assert r.status_code == 202


def test_gateway_health(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("method", ["get", "post"])
def test_v1_evaluate_is_gone_and_never_forwarded(client, method):
    kw = {"files": {"design_zip": ("d.zip", b"x" * 10)}} if method == "post" else {}
    r = getattr(client, method)("/evaluate", **kw)
    assert r.status_code == 410
    body = r.json()
    assert body["error"]["code"] == "V1_RETIRED" and body["error"]["retryable"] is False
    assert "/v2/evaluate" in body["error"]["message"]
    assert SEEN == []      # nothing reached the v2 service


def test_no_v1_services_referenced():
    src = (Path(main.__file__)).read_text()
    for name in ("verilator-api", "openlane-api", ":8001", ":8003"):
        assert name not in src


def test_evaluate_streams_evaluator_zip_with_design(client):
    evaluator = os.urandom(2 * 1024 * 1024)
    r = client.post('/v2/evaluate', files={
        'design_zip': ('design.zip', b'dummy design'),
        'evaluator_zip': ('evaluator.zip', evaluator),
        'evaluator_sig': ('evaluator.sig', b'detached signature'),
    }, data={'request_id': 'r1'})
    assert r.status_code == 202
    assert SEEN[-1][1] == '/evaluate' and SEEN[-1][3] > len(evaluator)


def test_unsigned_override_headers_preserved(client, monkeypatch):
    def reply(self):
        self._send(200, RAW, [('X-CF-V2-Allow-Unsigned', '1'), ('X-CF-Evaluator-Unsigned', '1'),
                             ('X-CF-Phase', 'done'), ('X-CF-Scope-Backend', 'cfscope')])
    monkeypatch.setattr(Fake, 'do_GET', reply)
    r = client.get('/v2/status/done')
    assert r.content == RAW
    assert r.headers['x-cf-v2-allow-unsigned'] == '1'
    assert r.headers['x-cf-evaluator-unsigned'] == '1'
    assert r.headers['x-cf-phase'] == 'done'
    assert r.headers['x-cf-scope-backend'] == 'cfscope'
