# failclosed

[![install](https://img.shields.io/badge/install-from%20GitHub-blue)](https://github.com/nickharris808/failclosed#install)
[![CI](https://img.shields.io/badge/ci-passing-brightgreen)](https://github.com/nickharris808/failclosed/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-57%20passing-brightgreen)](tests/)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![deps](https://img.shields.io/badge/dependencies-1-brightgreen)

**Default-deny ASGI middleware: a gated endpoint succeeds only on an affirmative machine-checked
verdict. Unknown is refused, not permitted.**

## Why this exists

Most authorization middleware is fail-*open* by accident, and the accident is always the same shape:
the check is written as "refuse if we found a problem" rather than "refuse unless we proved there
isn't one". Those differ on every path where the check did not complete — the handler threw, the
solver timed out, a refactor dropped the header — and on those paths a 200 goes out.

This inverts the default. On a gated path there is no code path from *undetermined* to *success*, and
each of those failure modes has a test.

Most authorization middleware is fail-*open* by accident. The handler throws, the solver times out,
someone forgets to stamp a header — and a 200 goes out anyway. `failclosed` inverts that: on a gated
path there is **no code path from "we could not determine safety" to a success status**.

~301 lines. One dependency (`starlette`), so it works with FastAPI too.

## Install

```
# from GitHub (PyPI release pending)
pip install "failclosed @ git+https://github.com/nickharris808/failclosed.git"
```

> `pip install failclosed` will work once the PyPI release lands. The distribution is built and `twine check`-clean; publication is pending.

## 30-second quickstart

```python
from failclosed import FailClosedMiddleware

app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",))
```

Now every response under `/verify` must carry `X-Verdict: SAFE` or it becomes a 403.

```python
from starlette.responses import JSONResponse

def handler(request):
    proved = my_solver.check(request)             # True / False / None
    verdict = "SAFE" if proved else ("UNSAFE" if proved is False else "REFUSED")
    return JSONResponse({"detail": "..."}, headers={"X-Verdict": verdict})
```

## What happens on each branch

| Handler does | Result |
|---|---|
| stamps `SAFE` | **200**, body verbatim |
| stamps `UNSAFE` | **403**, original body preserved + refusal metadata |
| stamps `REFUSED` (solver said unknown, or is not installed) | **403** |
| returns 200 with **no** verdict header | **403** — a gated success that was never certified |
| raises | **403**, not 500 |
| exceeds the deadline | **403** — a timed-out check is an undetermined one |
| path is **not** gated | passes through untouched, no deadline, no buffering |
| returns 404/422 on a gated path | passes through as itself — a routing answer is not a failed check |

## Example output

```console
$ curl -i localhost:8000/verify/unsafe
HTTP/1.1 403 Forbidden
x-verdict: UNSAFE

{"counterexample": ["s0", "s1"],
 "refused": true,
 "refusal_verdict": "UNSAFE",
 "refusal_reason": "conclusive counterexample (not proven safe)"}
```

The counterexample survives the refusal. You lose the status code, never the diagnosis.

## Design notes

**Nothing is gated by default.** `gated_prefixes` defaults to `()`, so installing the middleware
without configuring it cannot silently change your app's behaviour. You opt paths in explicitly.

**The deadline is a refusal, not a 504.** A verification that ran out of time did not answer the
question, and an unanswered question is not a pass.

**Warm your solver at construction**, so a cold first request does not spend its whole budget on a
shared-library load:

```python
def warm():
    import z3; z3.Solver().check()

app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",), warm=warm)
```

**Configurable header** via `verdict_header=` if `X-Verdict` collides with something you already use.

## Honest scope

**What it guarantees.** On a gated path, a status below 400 is returned only when the handler stamped
an affirmative `SAFE` verdict *and* the response body arrived intact within the deadline. There is no
code path from "could not determine" to success.

The deadline covers the **whole exchange** — handler dispatch and reading the response body. Wrapping
only the handler left streaming outside the budget, so a handler that returned headers instantly and
then stalled blew through a 500 ms deadline and still returned 200.

**Completeness is required, not assumed.** A `SAFE` response must be one whose body can be confirmed
whole: it declares a `Content-Length` that matches, or it declares a JSON media type and parses. If
neither holds, the gate refuses and says so. This is because Starlette's `BaseHTTPMiddleware` records
a mid-stream handler exception but only re-raises it *after* the response has been sent — so
truncation is the only evidence available at gate time. Pass `require_verifiable_body=False` to
accept that risk deliberately for a genuinely unbounded stream.

**What it does not do.**

- It does not decide the verdict. Mapping your solver's output — or your endpoint's response shape —
  to SAFE/UNSAFE/REFUSED is your handler's job, because that mapping is specific to what you are
  proving. `failclosed` enforces the *consequence*, which is the part everyone gets wrong.
- It does not verify that a `SAFE` stamp was *earned*. A handler that stamps `SAFE` unconditionally
  will pass. The gate enforces that an unstamped or negatively-stamped response cannot succeed; it
  cannot audit your prover for you.
- It does not protect ungated paths, and gating is prefix-matched literally — `/g` also gates
  `/ghost`. Choose prefixes that cannot collide.
- It buffers gated response bodies up to `max_body_bytes` (8 MiB default), so gated endpoints are not
  suitable for large downloads.

**A bypass shipped in 0.1.0 and is fixed here.** The deadline did not cover body streaming. See
[SECURITY-ADVISORY.md](SECURITY-ADVISORY.md).

If you want the other half — a verification engine that produces those verdicts — see
[`minicheck`](https://github.com/nickharris808/minicheck), an explicit-state model checker with no required dependencies.

`failclosed` is the enforcement point extracted from a production verification gate. The gate itself
— the solver fleet behind it, the response classifiers that map each endpoint's shape to a verdict,
and the evidence trail that makes a verdict auditable after the fact — is the commercial offering.
This middleware is MIT and always will be.

## Tests

```
pip install -e ".[test]" && pytest
```

57 tests, one per branch in the table above, each driving a real ASGI app.

## The portfolio

Five small, independently useful tools built around one idea: **a verdict you cannot check is not a verdict.**

| | |
|---|---|
| [`minicheck`](https://github.com/nickharris808/minicheck) | An explicit-state model checker in ~1308 lines. Shortest counterexamples, no required dependencies. |
| [`protocol-bench`](https://github.com/nickharris808/protocol-bench) | 15 published IEEE 802.11 / 3GPP procedures with ground truth. A claimed detection must **replay**. |
| [`minicheck-mcp`](https://github.com/nickharris808/minicheck-mcp) | The checker as an **MCP server** — let an agent verify a state machine instead of guessing. |
| [`polyfrac`](https://github.com/nickharris808/polyfrac) | Exact polynomial + rational-function arithmetic over ℚ with Sturm real-root counting. Zero deps. |
| [`failclosed`](https://github.com/nickharris808/failclosed) ← *you are here* | Default-deny ASGI middleware: a gated endpoint succeeds only on an affirmative verdict. |
| [`protocol-bench-action`](https://github.com/nickharris808/protocol-bench-action) | Score a submission in CI and fail the build if a claimed detection cannot be proved |

Try it in your browser: **[live demo](https://huggingface.co/spaces/nickh007/protocol-bench-demo)** · Ground-truth tasks: **[dataset](https://huggingface.co/datasets/nickh007/protocol-bench)**

### The commercial offering

These are the engine. What is **not** open source is what makes it useful at scale: the maintained
hazard-property corpora, composition analysis that finds hazards existing only when two components
are combined, the trust-model sensitivity sweep, and the evidence trail that makes a verdict auditable
after the fact. The tools above are MIT and stay that way.

## Licence

MIT. See `LICENSE`.
