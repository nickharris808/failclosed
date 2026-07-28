"""failclosed — default-deny ASGI middleware for verification-gated endpoints.

A gated endpoint returns its success status ONLY when the handler stamped a machine-checked
SAFE verdict header. Everything else is refused with HTTP 403:

  * a conclusive UNSAFE verdict,
  * an inconclusive one (a solver returning `unknown`, or the solver being unavailable),
  * a missing verdict on a gated path,
  * a wall-clock timeout,
  * an unhandled exception in the handler.

The diagnostic body is preserved inside the 403 — nothing is lost; only the status signals
"did not pass the gate". Ungated paths pass through untouched, with no deadline and no
body buffering.

The design rule is that there is no code path from "we could not determine safety" to a
success status. Absence of evidence is refused, not permitted.
"""

from __future__ import annotations

import asyncio
import json
from enum import Enum

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

__all__ = [
    "Verdict",
    "normalize",
    "FailClosedMiddleware",
    "DEFAULT_VERDICT_HEADER",
    "DEFAULT_DEADLINE_S",
]

#: HTTP header a handler stamps with its machine-checked verdict.
DEFAULT_VERDICT_HEADER = "X-Verdict"

#: Wall-clock backstop for a gated request, in seconds.
DEFAULT_DEADLINE_S = 0.5


class Verdict(str, Enum):
    """The three-valued verdict. Only SAFE passes the gate."""

    SAFE = "SAFE"  #: a machine-checked proof of safety
    UNSAFE = "UNSAFE"  #: a conclusive counterexample
    REFUSED = "REFUSED"  #: unknown, unavailable, timed out, missing, or unrecognised


def normalize(safe: bool | None) -> Verdict:
    """The core rule: ``True`` -> SAFE, ``False`` -> UNSAFE, ``None`` -> REFUSED.

    ``None`` means the question was asked and not answered — a solver returning ``unknown``,
    or a solver that is not installed. That is refused, never permitted.
    """
    if safe is True:
        return Verdict.SAFE
    if safe is False:
        return Verdict.UNSAFE
    return Verdict.REFUSED


class FailClosedMiddleware(BaseHTTPMiddleware):
    """Refuse any gated response that is not an affirmative machine-checked SAFE.

    :param app: the ASGI application.
    :param gated_prefixes: path prefixes governed by the gate. A request whose path starts with
        any of these must carry a SAFE verdict to succeed. Everything else passes through
        untouched. Defaults to ``()`` — nothing gated — so that installing the middleware
        without configuring it cannot silently change behaviour.
    :param deadline_s: wall-clock budget for a gated request. Exceeding it is a refusal, not
        a 504, because a timed-out verification is an undetermined one.
    :param verdict_header: the header the handler stamps.
    :param warm: optional zero-argument callable invoked once at construction. Use it to load a
        solver library so a cold first request does not spend its deadline on import time.
    """

    def __init__(
        self,
        app,
        gated_prefixes: tuple[str, ...] = (),
        deadline_s: float = DEFAULT_DEADLINE_S,
        verdict_header: str = DEFAULT_VERDICT_HEADER,
        warm=None,
    ):
        super().__init__(app)
        self.gated_prefixes = tuple(gated_prefixes)
        self.deadline_s = deadline_s
        self.verdict_header = verdict_header
        if warm is not None:
            try:
                warm()
            except Exception:
                # A failed warm-up must not prevent start-up; the gate stays fail-closed either way.
                pass

    def is_gated(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.gated_prefixes)

    async def dispatch(self, request, call_next):
        if not self.is_gated(request.url.path):
            return await call_next(request)

        try:
            response = await asyncio.wait_for(call_next(request), timeout=self.deadline_s)
        except asyncio.TimeoutError:
            return self._refuse(
                Verdict.REFUSED,
                f"no verdict within the {int(self.deadline_s * 1000)}ms fail-closed deadline",
                None,
            )
        except Exception:
            # A handler error must never read as success.
            return self._refuse(Verdict.REFUSED, "handler raised before returning a verdict", None)

        verdict = response.headers.get(self.verdict_header)
        if verdict is None:
            # A gated success without a verdict cannot be confirmed safe. A 4xx/5xx is a genuine
            # routing or input error and is passed through as itself.
            if response.status_code < 400:
                return self._refuse(Verdict.REFUSED, "gated endpoint returned no machine-checked verdict", None)
            return response

        body = await _read_body(response)
        if verdict == Verdict.SAFE.value:
            # Re-emit verbatim; the original body iterator has been consumed.
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        reason = (
            "conclusive counterexample (not proven safe)"
            if verdict == Verdict.UNSAFE.value
            else "safety could not be machine-checked (unknown or unavailable)"
        )
        return self._refuse(verdict, reason, payload if isinstance(payload, dict) else {})

    def _refuse(self, verdict, reason: str, body: dict | None) -> JSONResponse:
        v = str(getattr(verdict, "value", verdict))
        payload = dict(body) if isinstance(body, dict) else {}
        payload["refused"] = True
        payload["refusal_reason"] = reason
        payload["refusal_verdict"] = v
        resp = JSONResponse(status_code=403, content=payload)
        resp.headers[self.verdict_header] = v
        return resp


async def _read_body(response) -> bytes:
    if hasattr(response, "body_iterator"):
        chunks = [chunk async for chunk in response.body_iterator]
        return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    return getattr(response, "body", b"") or b""
