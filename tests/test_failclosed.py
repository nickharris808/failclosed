"""Tests for failclosed.

The single property under test is that there is NO path from "we could not determine safety"
to a success status. Each test drives a real ASGI app through Starlette's test client.
"""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from failclosed import DEFAULT_VERDICT_HEADER, FailClosedMiddleware, Verdict, normalize


# --------------------------------------------------------------------------- the rule itself
def test_normalize_is_three_valued():
    assert normalize(True) is Verdict.SAFE
    assert normalize(False) is Verdict.UNSAFE
    assert normalize(None) is Verdict.REFUSED


def test_verdict_is_a_str_enum_so_it_serialises():
    assert Verdict.SAFE == "SAFE"
    assert Verdict.SAFE.value == "SAFE"


# --------------------------------------------------------------------------- app fixture
def build_app(deadline_s=0.5, gated=("/verify",)):
    """One app exposing every branch the middleware has to handle."""

    def safe(request):
        return JSONResponse({"detail": "proved"}, headers={DEFAULT_VERDICT_HEADER: "SAFE"})

    def unsafe(request):
        return JSONResponse({"counterexample": ["s0", "s1"]}, headers={DEFAULT_VERDICT_HEADER: "UNSAFE"})

    def unknown(request):
        return JSONResponse({"detail": "solver said unknown"}, headers={DEFAULT_VERDICT_HEADER: "REFUSED"})

    def unstamped(request):
        return JSONResponse({"detail": "forgot to stamp"})

    def not_found(request):
        return JSONResponse({"detail": "no such protocol"}, status_code=404)

    def boom(request):
        raise RuntimeError("verifier exploded")

    async def slow(request):
        await asyncio.sleep(2.0)
        return JSONResponse({"detail": "too late"}, headers={DEFAULT_VERDICT_HEADER: "SAFE"})

    def ungated(request):
        return PlainTextResponse("open")

    app = Starlette(
        routes=[
            Route("/verify/safe", safe),
            Route("/verify/unsafe", unsafe),
            Route("/verify/unknown", unknown),
            Route("/verify/unstamped", unstamped),
            Route("/verify/notfound", not_found),
            Route("/verify/boom", boom),
            Route("/verify/slow", slow),
            Route("/open", ungated),
        ]
    )
    app.add_middleware(FailClosedMiddleware, gated_prefixes=gated, deadline_s=deadline_s)
    return app


@pytest.fixture()
def client():
    return TestClient(build_app(), raise_server_exceptions=False)


# --------------------------------------------------------------------------- the one path that passes
def test_safe_verdict_passes_through_with_its_body_intact(client):
    r = client.get("/verify/safe")
    assert r.status_code == 200
    assert r.json() == {"detail": "proved"}
    assert r.headers[DEFAULT_VERDICT_HEADER] == "SAFE"


# --------------------------------------------------------------------------- every path that does not
def test_unsafe_is_refused_and_the_diagnostic_body_survives(client):
    r = client.get("/verify/unsafe")
    assert r.status_code == 403
    body = r.json()
    assert body["refused"] is True
    assert body["refusal_verdict"] == "UNSAFE"
    assert body["counterexample"] == ["s0", "s1"]  # diagnostic preserved, not discarded


def test_unknown_verdict_is_refused(client):
    r = client.get("/verify/unknown")
    assert r.status_code == 403
    assert r.json()["refusal_verdict"] == "REFUSED"
    assert "could not be machine-checked" in r.json()["refusal_reason"]


def test_missing_verdict_on_a_gated_success_is_refused(client):
    """The defence-in-depth case: a handler that returns 200 but forgets to stamp."""
    r = client.get("/verify/unstamped")
    assert r.status_code == 403
    assert "no machine-checked verdict" in r.json()["refusal_reason"]


def test_handler_exception_is_refused_not_500(client):
    r = client.get("/verify/boom")
    assert r.status_code == 403
    assert r.json()["refusal_reason"] == "handler raised before returning a verdict"


def test_timeout_is_refused():
    c = TestClient(build_app(deadline_s=0.05), raise_server_exceptions=False)
    r = c.get("/verify/slow")
    assert r.status_code == 403
    assert "fail-closed deadline" in r.json()["refusal_reason"]


# --------------------------------------------------------------------------- pass-through behaviour
def test_ungated_path_is_untouched(client):
    r = client.get("/open")
    assert r.status_code == 200
    assert r.text == "open"
    assert DEFAULT_VERDICT_HEADER not in r.headers


def test_gated_error_status_passes_through_as_itself(client):
    """A 404 is a routing answer, not a failed safety check — it must not become a 403."""
    r = client.get("/verify/notfound")
    assert r.status_code == 404
    assert r.json()["detail"] == "no such protocol"


def test_nothing_is_gated_by_default():
    """Installing the middleware without configuring prefixes must not change behaviour."""
    app = Starlette(routes=[Route("/verify/unstamped", lambda r: JSONResponse({"detail": "hi"}))])
    app.add_middleware(FailClosedMiddleware)  # no gated_prefixes
    r = TestClient(app).get("/verify/unstamped")
    assert r.status_code == 200


# --------------------------------------------------------------------------- configuration
def test_custom_header_is_honoured():
    def h(request):
        return JSONResponse({"ok": True}, headers={"X-Proof": "SAFE"})

    app = Starlette(routes=[Route("/verify/x", h)])
    app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",), verdict_header="X-Proof")
    r = TestClient(app).get("/verify/x")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_custom_header_means_the_default_header_no_longer_passes():
    def h(request):
        return JSONResponse({"ok": True}, headers={DEFAULT_VERDICT_HEADER: "SAFE"})

    app = Starlette(routes=[Route("/verify/x", h)])
    app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",), verdict_header="X-Proof")
    r = TestClient(app).get("/verify/x")
    assert r.status_code == 403  # stamped the wrong header -> undetermined -> refused


def test_multiple_prefixes_are_all_gated():
    def unstamped(request):
        return JSONResponse({"detail": "hi"})

    app = Starlette(routes=[Route("/a/x", unstamped), Route("/b/x", unstamped), Route("/c/x", unstamped)])
    app.add_middleware(FailClosedMiddleware, gated_prefixes=("/a", "/b"))
    c = TestClient(app)
    assert c.get("/a/x").status_code == 403
    assert c.get("/b/x").status_code == 403
    assert c.get("/c/x").status_code == 200


def test_warm_callable_runs_once_and_a_failing_one_does_not_break_startup():
    calls = []
    app = Starlette(routes=[Route("/open", lambda r: PlainTextResponse("ok"))])
    app.add_middleware(FailClosedMiddleware, warm=lambda: calls.append(1))
    assert TestClient(app).get("/open").status_code == 200
    assert calls == [1]

    app2 = Starlette(routes=[Route("/open", lambda r: PlainTextResponse("ok"))])

    def bad_warm():
        raise RuntimeError("solver missing")

    app2.add_middleware(FailClosedMiddleware, warm=bad_warm)  # must not raise
    assert TestClient(app2).get("/open").status_code == 200
