# failclosed

[![install](https://img.shields.io/badge/install-from%20GitHub-blue)](https://github.com/nickharris808/failclosed#install)
[![CI](https://img.shields.io/badge/ci-passing-brightgreen)](https://github.com/nickharris808/failclosed/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-62%20passing-brightgreen)](tests/)
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

This inverts the default. On a gated path there is **no code path from "we could not determine
safety" to a success status**, and each of those failure modes has a test.

~301 lines. One dependency (`starlette`), so it works with FastAPI too.

## Install

```
# from GitHub (PyPI release pending)
pip install "failclosed @ git+https://github.com/nickharris808/failclosed.git"
```

> `pip install failclosed` does not work yet — the package is not on PyPI. Install from GitHub as
> shown above. The distribution builds and is `twine check`-clean, with no unpublished
> dependencies, so it is ready to upload whenever that happens.

## 30-second quickstart

Complete and runnable — paste it into a file and run it.

```python
from failclosed import FailClosedMiddleware, normalize
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

def check(request):                                   # your solver: True / False / None
    return {"yes": True, "no": False}.get(request.query_params.get("a"))

def handler(request):
    proved = check(request)
    return JSONResponse({"proved": proved}, headers={"X-Verdict": normalize(proved).value})

app = Starlette(routes=[Route("/verify/thing", handler)])
app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify",))

client = TestClient(app)
print(client.get("/verify/thing?a=yes").status_code)   # 200  <- proved safe
print(client.get("/verify/thing?a=no").status_code)    # 403  <- counterexample
print(client.get("/verify/thing").status_code)         # 403  <- solver said None
```

Every response under `/verify` must now carry `X-Verdict: SAFE` or it becomes a 403. The third
line is the one that matters: nothing went wrong, the solver simply could not tell — and that is
refused rather than passed.

## Tutorial — gating a real endpoint

End to end, from an ungated app to one where an unverified response cannot succeed.

**1. Start with an endpoint that decides something.** It calls a checker and returns the answer:

```python
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

def deploy(request):
    ok = my_checker.is_safe(request)     # True / False / None
    return JSONResponse({"deployed": ok})

app = Starlette(routes=[Route("/verify/deploy", deploy, methods=["POST"])])
```

The bug is not visible yet. If `my_checker` raises, or returns `None` because a solver timed out,
this still returns **200** with `{"deployed": null}` — and whatever reads it sees a success.

**2. Stamp the verdict.** `normalize` maps the three-valued answer onto the header:

```python
from failclosed import normalize

def deploy(request):
    ok = my_checker.is_safe(request)
    return JSONResponse({"deployed": ok}, headers={"X-Verdict": normalize(ok).value})
```

**3. Install the gate.**

```python
from failclosed import FailClosedMiddleware

app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify/",), deadline_s=2.0)
```

**4. Check each branch.** These are the four that matter, and all four have tests:

| what happens | before the gate | after |
|---|---|---|
| checker proves safe | 200 | **200**, body verbatim |
| checker finds a counterexample | 200 with `false` | **403**, counterexample preserved in the body |
| solver times out, returns `None` | 200 with `null` | **403**, `"safety could not be machine-checked"` |
| the handler raises | 500 | **403** — never a success, and never a stack trace |

**5. The failure mode you did not write.** Someone later refactors the handler and drops the header.
Without the gate that is a silent 200. With it:

```console
$ curl -i -X POST localhost:8000/verify/deploy
HTTP/1.1 403 Forbidden
x-verdict: REFUSED

{"refused": true, "refusal_verdict": "REFUSED",
 "refusal_reason": "gated endpoint returned no machine-checked verdict"}
```

That is the whole point: the gate does not need to know *why* the verdict is missing. Absent
evidence is refused.

**6. Keep the diagnosis.** A refusal preserves the original body and adds to it, so you lose the
status code and never the reason. Log `refusal_reason`; it names which of the branches above fired.

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

## API reference

### `FailClosedMiddleware`

| Parameter | Default | What it does |
|---|---|---|
| `gated_prefixes` | `()` | Path prefixes the gate governs. Matched with `str.startswith`, so `/g` also gates `/ghost`. Empty by default: installing the middleware cannot silently change behaviour. |
| `deadline_s` | `0.5` | Wall-clock budget for the **whole** gated exchange, handler dispatch and response body. Exceeding it is a refusal, not a 504. |
| `max_body_bytes` | `8 MiB` | Largest gated response body buffered before refusing. |
| `require_verifiable_body` | `True` | A `SAFE` response must be confirmably complete — a matching `Content-Length`, or a JSON media type that parses. Set `False` to accept unverifiable bodies knowingly. |
| `verdict_header` | `"X-Verdict"` | The header the handler stamps. |
| `warm` | `None` | Zero-argument callable run once at construction, e.g. to import a solver so a cold first request does not spend its deadline on it. A failure here is swallowed; the gate stays fail-closed either way. |

### `Verdict`

A `str` enum. Only `SAFE` passes; matching is exact, with no case folding or trimming.

| Member | Value | Meaning |
|---|---|---|
| `Verdict.SAFE` | `"SAFE"` | a machine-checked proof of safety |
| `Verdict.UNSAFE` | `"UNSAFE"` | a conclusive counterexample |
| `Verdict.REFUSED` | `"REFUSED"` | unknown, unavailable, timed out, missing, or unrecognised |

### `normalize(safe: bool | None) -> Verdict`

The mapping rule: `True` → `SAFE`, `False` → `UNSAFE`, **`None` → `REFUSED`**. `None` means the
question was asked and not answered — a solver returning `unknown`, or one that is not installed.

### `BodyTooLarge`

Raised internally when a gated body exceeds `max_body_bytes`; surfaces to the client as a 403 with
the reason stated. You will not normally catch it.

### The refusal body

Every 403 the gate produces is JSON containing the original body's keys (when it was a JSON object)
plus `refused: true`, `refusal_reason`, and `refusal_verdict`. The diagnosis is never discarded —
you lose the status code, not the evidence.

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

## Troubleshooting

**Everything under my gated prefix returns 403.** The handler is not stamping the header, or is
stamping something other than the exact string `SAFE`. Matching is exact — no case folding, no
trimming. Check `response.headers["X-Verdict"]`.

**A 403 with `"the response streams without a Content-Length and without a JSON media type"`.** The
gate cannot confirm the body arrived whole, so it refuses. Declare a `Content-Length`, use a JSON
media type, or pass `require_verifiable_body=False` if you accept the risk on that route.

**A 403 with `"did not parse; it is likely truncated"`.** The handler died part-way through
streaming. Starlette re-raises that exception only after the response is sent, so truncation is the
only evidence available at gate time — and a half-payload stamped SAFE is worse than a refusal.

**Timeouts on a route that used to pass.** The deadline covers the *whole* exchange including the
response body, not just handler dispatch. A handler that returns headers immediately and streams
slowly now counts against the budget. Raise `deadline_s` if the work genuinely takes that long.

**`/ghost` is being gated and I only meant `/g`.** Prefixes are matched literally with
`str.startswith`. Use `/g/` or a prefix that cannot collide.

**Nothing is gated at all.** `gated_prefixes` defaults to `()`. That is deliberate — installing the
middleware without configuring it must not silently change behaviour.

**A gated download is refused with `"body exceeded"`.** Gated responses are buffered up to
`max_body_bytes` (8 MiB). Gated endpoints are not for large downloads; put the download on an
ungated path.

## Performance

The middleware buffers a gated response body and compares one header. There is no measured
bottleneck here and nothing has been optimised — the wall-clock cost of a gated request is
dominated by your handler and your solver, not by this code.

## Tests

```
pip install -e ".[test]" && pytest
```

62 tests, one per branch in the table above, each driving a real ASGI app.

## The portfolio

Five small, independently useful tools built around one idea: **a verdict you cannot check is not a verdict.**

| | |
|---|---|
| [`minicheck`](https://github.com/nickharris808/minicheck) | An explicit-state model checker with a CLI. Shortest counterexamples, no required dependencies. |
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
