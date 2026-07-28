"""failclosed — default-deny ASGI middleware for verification-gated endpoints.

A gated endpoint returns its success status ONLY when the handler stamped a machine-checked
SAFE verdict header. Everything else is refused with HTTP 403:

  * a conclusive UNSAFE verdict,
  * an inconclusive one (a solver returning `unknown`, or the solver being unavailable),
  * a missing verdict on a gated path,
  * a wall-clock timeout — measured across the whole exchange, including streaming the body,
  * a response body larger than the buffering cap,
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
    "BodyTooLarge",
    "DEFAULT_VERDICT_HEADER",
    "DEFAULT_DEADLINE_S",
    "DEFAULT_MAX_BODY_BYTES",
]

#: HTTP header a handler stamps with its machine-checked verdict.
DEFAULT_VERDICT_HEADER = "X-Verdict"

#: Wall-clock backstop for a gated request, in seconds. Covers the whole exchange, body included.
DEFAULT_DEADLINE_S = 0.5

#: Largest gated response body that will be buffered before the gate refuses. 8 MiB.
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024


class BodyTooLarge(Exception):
    """A gated response body exceeded the buffering cap, so no verdict could be confirmed."""


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
    :param deadline_s: wall-clock budget for the whole gated exchange — handler dispatch *and*
        reading the response body. Exceeding it is a refusal, not a 504, because a timed-out
        verification is an undetermined one.
    :param max_body_bytes: largest gated response body buffered before refusing. Prevents an
        endless body from exhausting memory inside the component whose job is to fail closed.
    :param require_verifiable_body: when True (the default), a SAFE response must be one whose
        completeness can be confirmed — it declares a Content-Length, or it declares a JSON media
        type and parses. A body that could be silently truncated is refused rather than passed on.
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
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        require_verifiable_body: bool = True,
        warm=None,
    ):
        super().__init__(app)
        self.gated_prefixes = tuple(gated_prefixes)
        self.deadline_s = deadline_s
        self.verdict_header = verdict_header
        self.max_body_bytes = max_body_bytes
        self.require_verifiable_body = require_verifiable_body
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
            # The deadline covers the WHOLE gated exchange, body included. Wrapping only
            # `call_next` left the response body outside the budget, so a handler that returned
            # headers instantly and then streamed slowly blew through a 500ms deadline and still
            # returned 200. The clock has to run until there is a complete answer to gate.
            return await asyncio.wait_for(
                self._gated(request, call_next),
                timeout=self.deadline_s,
            )
        except asyncio.TimeoutError:
            return self._refuse(
                Verdict.REFUSED,
                f"no verdict within the {int(self.deadline_s * 1000)}ms fail-closed deadline",
                None,
            )
        except BodyTooLarge as e:
            return self._refuse(Verdict.REFUSED, str(e), None)
        except Exception:
            # A handler error must never read as success.
            return self._refuse(Verdict.REFUSED, "handler raised before returning a verdict", None)

    async def _gated(self, request, call_next):
        """The gated path, in full, so a single deadline can cover all of it."""
        response = await call_next(request)

        verdict = response.headers.get(self.verdict_header)
        if verdict is None:
            # A gated success without a verdict cannot be confirmed safe. A 4xx/5xx is a genuine
            # routing or input error and is passed through as itself.
            if response.status_code < 400:
                return self._refuse(Verdict.REFUSED, "gated endpoint returned no machine-checked verdict", None)
            return response

        body = await _read_body(response, self.max_body_bytes)
        if verdict == Verdict.SAFE.value:
            incomplete = _incompleteness(response, body, self.require_verifiable_body)
            if incomplete is not None:
                # A SAFE verdict on a body that did not arrive intact is not something we can pass
                # on. The usual cause is a handler that raised part-way through streaming: Starlette
                # records the exception but only re-raises it after the response has been sent, so
                # without this check the client receives a truncated payload stamped SAFE.
                return self._refuse(Verdict.REFUSED, f"verdict was SAFE but {incomplete}", None)
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


def _incompleteness(response, body: bytes, require_verifiable: bool = True) -> str | None:
    """Return a reason string if the body cannot be confirmed intact, else None.

    The failure this guards against is a gated handler that dies part-way through producing its
    response. Starlette's `BaseHTTPMiddleware` records that exception but only re-raises it *after*
    the response has been sent, so by the time anything can act on it the client already holds a
    truncated payload stamped SAFE. Truncation is the only evidence available at this point.

    Completeness is established one of two ways:

    1. **Declared length.** The handler set ``Content-Length`` and the body matches it.
    2. **JSON integrity.** The handler declared a JSON media type and the body parses. A
       verification endpoint's payload *is* its evidence, so half of one is not evidence at all.

    If neither applies, completeness is *unknown*, and an unknown does not pass a fail-closed gate —
    the response is refused with an explanation of how to make it checkable. Set
    ``require_verifiable_body=False`` to accept that risk deliberately for a prefix that streams
    something genuinely unbounded.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            expected = int(declared)
        except ValueError:
            return f"the Content-Length header {declared!r} is not a number"
        if len(body) != expected:
            return f"the body is {len(body)} bytes but Content-Length declared {expected}"
        return None

    media = (response.media_type or response.headers.get("content-type") or "").lower()
    if "json" in media:
        if not body.strip():
            return None  # an empty body is complete
        try:
            json.loads(body)
        except ValueError as e:
            return f"the declared JSON body did not parse ({e}); it is likely truncated"
        return None

    if require_verifiable:
        return (
            "the response streams without a Content-Length and without a JSON media type, so the "
            "gate cannot confirm the body arrived intact. Declare a Content-Length, use a JSON "
            "media type, or pass require_verifiable_body=False to accept unverifiable bodies"
        )
    return None


async def _read_body(response, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> bytes:
    """Buffer a response body, refusing past `max_bytes`.

    Unbounded before: a gated handler streaming an endless body grew this list until the process
    died. Since the middleware exists to keep an unverified response from succeeding, exhausting
    memory inside it is the one failure that defeats the whole design.

    The wall-clock side is handled by the caller's deadline, which now spans this read.
    """
    if hasattr(response, "body_iterator"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.body_iterator:
            b = chunk if isinstance(chunk, bytes) else chunk.encode()
            total += len(b)
            if total > max_bytes:
                raise BodyTooLarge(
                    f"gated response body exceeded {max_bytes} bytes before a verdict could be "
                    f"confirmed; refused rather than buffered"
                )
            chunks.append(b)
        return b"".join(chunks)
    body = getattr(response, "body", b"") or b""
    if len(body) > max_bytes:
        raise BodyTooLarge(f"gated response body of {len(body)} bytes exceeds the {max_bytes}-byte cap")
    return body
