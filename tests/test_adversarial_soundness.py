"""Hostile-HTTP suite: no request may reach a success status without an affirmative SAFE verdict.

The middleware's one job is that "we could not determine safety" never becomes 200. These tests
attack that from every direction the network allows — stalls, floods, malformed headers, handlers
that die mid-stream, and verdicts that are almost but not quite right.

The oracle is a single invariant, asserted after every case: **status < 400 implies the response
carried a genuine SAFE verdict**.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from failclosed import DEFAULT_MAX_BODY_BYTES, FailClosedMiddleware, Verdict


def build(routes, **kw):
    app = Starlette(routes=routes)
    app.add_middleware(FailClosedMiddleware, gated_prefixes=("/g",), **kw)
    return TestClient(app, raise_server_exceptions=False)


def assert_gate_invariant(response):
    """The one rule: a success status must be backed by an affirmative SAFE verdict."""
    if response.status_code < 400:
        assert response.headers.get("X-Verdict") == Verdict.SAFE.value, (
            f"status {response.status_code} without a SAFE verdict — the gate leaked"
        )


# ------------------------------------------------------------------------- C5: the deadline hole
def test_a_slow_body_cannot_outrun_the_deadline():
    """The shipped middleware returned 200 after 3.0s under a 0.5s deadline."""

    async def slow(request):
        async def gen():
            yield b'{"partial":'
            await asyncio.sleep(3.0)
            yield b'"done"}'

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"}, media_type="application/json")

    client = build([Route("/g/slow", slow)], deadline_s=0.5)
    start = time.time()
    r = client.get("/g/slow")
    elapsed = time.time() - start
    assert r.status_code == 403
    assert elapsed < 2.0, f"deadline did not bound the body read ({elapsed:.2f}s)"
    assert "deadline" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


def test_a_slow_handler_still_times_out():
    async def slow(request):
        await asyncio.sleep(3.0)
        return JSONResponse({}, headers={"X-Verdict": "SAFE"})

    client = build([Route("/g/slow", slow)], deadline_s=0.3)
    r = client.get("/g/slow")
    assert r.status_code == 403
    assert_gate_invariant(r)


def test_a_body_that_never_ends_is_refused_not_buffered_forever():
    async def flood(request):
        async def gen():
            while True:
                yield b"x" * 65536

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"})

    client = build([Route("/g/flood", flood)], deadline_s=30.0, max_body_bytes=1_000_000)
    r = client.get("/g/flood")
    assert r.status_code == 403
    assert "exceeded" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


def test_body_cap_default_is_finite():
    assert 0 < DEFAULT_MAX_BODY_BYTES < 1024 * 1024 * 1024


def test_a_body_just_under_the_cap_passes():
    """The cap must not break legitimate large-but-bounded responses."""
    payload = b'{"pad": "' + b"y" * 5000 + b'"}'

    async def ok(request):
        async def gen():
            yield payload

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"}, media_type="application/json")

    client = build([Route("/g/ok", ok)], max_body_bytes=10_000)
    r = client.get("/g/ok")
    assert r.status_code == 200
    assert r.content == payload
    assert_gate_invariant(r)


def test_an_unverifiable_body_is_refused_by_default():
    """A stream with neither a Content-Length nor a JSON type cannot be confirmed complete."""

    async def h(request):
        async def gen():
            yield b"opaque binary payload"

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert "cannot confirm the body arrived intact" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


def test_the_unverifiable_body_check_can_be_opted_out_of_knowingly():
    """An explicit opt-out exists, so the strictness is a default and not a wall."""

    async def h(request):
        async def gen():
            yield b"opaque binary payload"

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"})

    client = build([Route("/g/h", h)], require_verifiable_body=False)
    r = client.get("/g/h")
    assert r.status_code == 200
    assert_gate_invariant(r)


def test_a_truncated_json_body_is_refused():
    """The concrete leak: a handler that dies mid-JSON used to return 200 with half a payload."""

    async def h(request):
        async def gen():
            yield b'{"start":1'
            raise RuntimeError("died mid-body")

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"}, media_type="application/json")

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert "truncated" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


def test_a_lying_content_length_is_refused():
    async def h(request):
        return Response(
            b"short",
            headers={"X-Verdict": "SAFE", "Content-Length": "9999"},
            media_type="application/octet-stream",
        )

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert "Content-Length" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


# ------------------------------------------------------------------------- verdicts and forgeries
@pytest.mark.parametrize(
    "verdict",
    ["UNSAFE", "REFUSED", "safe", "Safe", "SAFE ", " SAFE", "", "MAYBE", "TRUE", "1", "SAFE;SAFE", "ok"],
)
def test_only_the_exact_safe_token_passes(verdict):
    """Anything that is not exactly `SAFE` is refused. No case folding, no trimming, no aliases."""

    async def h(request):
        return JSONResponse({"data": 1}, headers={"X-Verdict": verdict})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    if verdict == "SAFE":
        assert r.status_code == 200
    else:
        assert r.status_code == 403, f"{verdict!r} was accepted as a pass"
    assert_gate_invariant(r)


def test_a_missing_verdict_on_a_2xx_is_refused():
    async def h(request):
        return JSONResponse({"looks": "fine"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert "no machine-checked verdict" in r.json()["refusal_reason"]
    assert_gate_invariant(r)


def test_a_handler_that_raises_is_refused_not_500ed_into_success():
    async def boom(request):
        raise RuntimeError("handler exploded")

    client = build([Route("/g/boom", boom)])
    r = client.get("/g/boom")
    assert r.status_code == 403
    assert_gate_invariant(r)


def test_a_handler_that_raises_mid_stream_is_refused():
    """Covered by the completeness requirement: an unverifiable body never passes.

    Starlette's `BaseHTTPMiddleware` swallows a mid-stream exception and only re-raises it after the
    response is sent, so the middleware cannot see the error directly — it can only observe that the
    body is not confirmably whole. That is enough to refuse, which is what matters.
    """

    async def h(request):
        async def gen():
            yield b'{"start":1'
            raise RuntimeError("died mid-body")

        return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert_gate_invariant(r)


def test_a_genuine_4xx_passes_through_as_itself():
    """A routing or input error is not a gate failure and must keep its own status."""

    async def h(request):
        return JSONResponse({"detail": "not found"}, status_code=404)

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 404
    assert_gate_invariant(r)


def test_unsafe_verdict_preserves_the_diagnostic_body():
    async def h(request):
        return JSONResponse({"counterexample": [1, 2, 3]}, headers={"X-Verdict": "UNSAFE"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    body = r.json()
    assert body["counterexample"] == [1, 2, 3]  # nothing lost
    assert body["refused"] is True
    assert_gate_invariant(r)


def test_a_non_json_body_on_a_refusal_does_not_crash():
    async def h(request):
        return PlainTextResponse("not json at all", headers={"X-Verdict": "UNSAFE"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert_gate_invariant(r)


def test_a_json_array_body_on_a_refusal_does_not_crash():
    async def h(request):
        return Response(b"[1,2,3]", media_type="application/json", headers={"X-Verdict": "UNSAFE"})

    client = build([Route("/g/h", h)])
    r = client.get("/g/h")
    assert r.status_code == 403
    assert_gate_invariant(r)


# ------------------------------------------------------------------------------ gating boundaries
def test_ungated_paths_are_untouched_and_undelayed():
    async def h(request):
        await asyncio.sleep(0.2)
        return JSONResponse({"free": True})

    client = build([Route("/open", h)], deadline_s=0.05)
    r = client.get("/open")
    assert r.status_code == 200  # no deadline, no verdict needed


def test_default_configuration_gates_nothing():
    """Installing the middleware without configuring it must not silently change behaviour."""

    async def h(request):
        return JSONResponse({"x": 1})

    app = Starlette(routes=[Route("/anything", h)])
    app.add_middleware(FailClosedMiddleware)
    r = TestClient(app).get("/anything")
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/g", "/g/", "/g/deep/nested", "/g?x=1", "/ghost"])
def test_prefix_matching_is_literal(path):
    """`/ghost` starts with `/g`, so it IS gated. Documenting that rather than pretending."""

    async def h(request):
        return JSONResponse({"x": 1})

    client = build([Route("/{p:path}", h)])
    r = client.get(path)
    assert r.status_code == 403  # every one of these is under the /g prefix
    assert_gate_invariant(r)


def test_a_warm_callable_that_raises_does_not_prevent_startup():
    async def h(request):
        return JSONResponse({"x": 1}, headers={"X-Verdict": "SAFE"})

    def bad_warm():
        raise RuntimeError("solver import failed")

    client = build([Route("/g/h", h)], warm=bad_warm)
    r = client.get("/g/h")
    assert r.status_code == 200  # gate still functions
    assert_gate_invariant(r)


def test_every_method_is_gated_equally():
    async def h(request):
        return JSONResponse({"x": 1})

    client = build([Route("/g/h", h, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])])
    for method in ("get", "post", "put", "delete", "patch"):
        r = getattr(client, method)("/g/h")
        assert r.status_code == 403
        assert_gate_invariant(r)


def test_a_large_request_body_does_not_bypass_the_gate():
    async def h(request):
        await request.body()
        return JSONResponse({"x": 1})

    client = build([Route("/g/h", h, methods=["POST"])])
    r = client.post("/g/h", content=b"z" * 2_000_000)
    assert r.status_code == 403
    assert_gate_invariant(r)
