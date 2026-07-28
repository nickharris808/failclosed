# Security advisory — deadline bypass via a slow response body (failclosed 0.1.0)

**Severity:** high — the bypass defeats the middleware's only purpose. **Fixed in:** 0.2.0.
**Found by:** the maintainer, during a hardening audit. **Exploitation:** none known.

## Summary

`FailClosedMiddleware` wrapped only `call_next` in the deadline. A gated handler that returned its
headers immediately and then streamed its body slowly spent unbounded wall-clock time **outside** the
budget, and the request still succeeded.

Measured on 0.1.0 with `deadline_s=0.5` and a handler stalling 3 seconds mid-body: **elapsed 3.01 s,
status 200.** The README stated that exceeding the deadline yields 403.

A second issue: `_read_body` had no size cap, so an endless gated body grew a list until the process
died — a denial of service inside the component whose job is to fail closed.

## Reproducer (0.1.0)

```python
async def slow(request):
    async def gen():
        yield b'{"partial":'
        await asyncio.sleep(3.0)
        yield b'"done"}'
    return StreamingResponse(gen(), headers={"X-Verdict": "SAFE"})

app.add_middleware(FailClosedMiddleware, gated_prefixes=("/g",), deadline_s=0.5)
# 0.1.0 -> 3.01s, status 200
```

## Fix

1. The deadline now covers the whole gated exchange, body read included.
2. `max_body_bytes` (8 MiB default) bounds buffering; exceeding it refuses.
3. A `SAFE` response must additionally be *confirmably complete* — a matching `Content-Length`, or a
   JSON media type that parses. This closes a related leak: Starlette re-raises a mid-stream handler
   exception only after the response has been sent, so a truncated body was previously delivered with
   status 200 and a `SAFE` header. Opt out with `require_verifiable_body=False`.

## Action required

Upgrade. If you gate an endpoint that streams a large non-JSON body, either declare a
`Content-Length` or set `require_verifiable_body=False` after considering the trade-off.
