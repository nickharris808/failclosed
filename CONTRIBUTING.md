# Contributing to failclosed

The point of this package is that it is small enough to audit and impossible to accidentally make
fail-open. That shapes what changes are easy to accept.

## Ground rules

1. **One dependency.** `starlette`, and nothing else at runtime. A pull request adding another will
   be declined regardless of merit.
2. **No new success path.** Every change is measured against one property: a gated request must not be
   able to reach a 2xx without an affirmative SAFE verdict. If your change adds a branch, add the test
   that proves the branch still refuses.
3. **The middleware does not classify.** Mapping a solver result or a response body to a verdict is
   the caller's job. Requests to add body-shape sniffing here will be declined — it makes the gate
   domain-specific and hides the decision.

## Getting set up

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest
```

## Pull requests

- Add a test that fails before your change and passes after. Tests live in `tests/`.
- Keep the public API in `__all__` explicit; anything not listed there is internal.
- Default-off stays default-off: `gated_prefixes` defaults to `()` on purpose.
- Sign-off by [DCO](https://developercertificate.org/) (`git commit -s`). There is no CLA.

## Reporting a fail-open

A gated request that reaches a 2xx without a SAFE verdict is the most serious possible bug here — it is
the one thing this package exists to prevent. Please report it privately first if you believe it is
exploitable, and include the route definition and middleware configuration so it can go straight into
the test suite.
