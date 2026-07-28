"""failclosed — default-deny ASGI middleware for verification-gated endpoints.

>>> from failclosed import FailClosedMiddleware, Verdict
>>> app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",))

A gated endpoint returns 200 only when the handler stamps ``X-Verdict: SAFE``. Unsafe, unknown,
missing, timed out, and raised are all 403. There is no path from "undetermined" to success.
"""

from ._core import (
    DEFAULT_DEADLINE_S,
    DEFAULT_VERDICT_HEADER,
    FailClosedMiddleware,
    Verdict,
    normalize,
)

__all__ = [
    "FailClosedMiddleware",
    "Verdict",
    "normalize",
    "DEFAULT_VERDICT_HEADER",
    "DEFAULT_DEADLINE_S",
    "__version__",
]
__version__ = "0.1.0"
