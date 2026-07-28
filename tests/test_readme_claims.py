"""The README must not contain a number the code cannot reproduce.

Documentation drift is the quiet member of the hallucination family: a claim that was true once, is
false now, and looks exactly as authoritative either way. Two counts in the shipped READMEs were
wrong like this — one said 23 tests against an actual 44, another said 61 against 72.

So the figures are re-derived here rather than trusted. Add a test or a source file, and if the
README disagrees this fails and names the number to write.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def collected_tests() -> int:
    """Ask pytest itself how many cases exist, so parametrisation is counted correctly."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    match = re.match(r"(\d+)", lines[-1]) if lines else None
    assert match, f"could not read a collection count from pytest:\n{out.stdout[-2000:]}"
    return int(match.group(1))


def source_lines() -> int:
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in sorted((ROOT / "src").rglob("*.py")))


def test_every_test_count_in_the_readme_is_the_real_one():
    actual = collected_tests()
    text = README.read_text(encoding="utf-8")

    badges = [int(m) for m in re.findall(r"tests-(\d+)", text)]
    assert badges, "README has no tests badge"
    for claimed in badges:
        assert claimed == actual, f"README badge says {claimed} tests; pytest collects {actual}"

    for claimed in [int(m) for m in re.findall(r"\b(\d+) tests\b", text)]:
        assert claimed == actual, f"README prose says {claimed} tests; pytest collects {actual}"


def test_line_count_claims_are_close_to_the_truth():
    """A "~N lines" claim about THIS package must be within 15% of its real size.

    Cross-links quote a sibling package's size, so a figure that matches no local file is only
    flagged when it is not attached to a link.
    """
    text = README.read_text(encoding="utf-8")
    actual = source_lines()
    for match in re.finditer(r"~(\d+) lines", text):
        claimed = int(match.group(1))
        if abs(claimed - actual) / max(actual, 1) <= 0.15:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line = text[line_start : text.find("\n", match.start())]
        assert "](https://github.com/" in line, f"README claims ~{claimed} lines but this package has {actual}"


def test_no_placeholder_text_shipped():
    text = README.read_text(encoding="utf-8").lower()
    for marker in ("todo", "fixme", "coming soon", "lorem ipsum", "placeholder"):
        assert marker not in text, f"README still contains {marker!r}"


def test_readme_states_what_the_tool_does_not_establish():
    """Every package must carry an explicit scope section. Silence about limits reads as absence."""
    text = README.read_text(encoding="utf-8")
    assert re.search(r"^#+ .*(honest scope|limitations|what this does not)", text, re.M | re.I), (
        "README has no section stating the tool's limits"
    )


# --------------------------------------------------------------- the tutorial must be reproducible
def _tutorial_client(behaviour):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from failclosed import FailClosedMiddleware, normalize

    def deploy(request):
        if behaviour == "raise":
            raise RuntimeError("checker exploded")
        if behaviour == "noheader":
            return JSONResponse({"deployed": None})
        ok = {"safe": True, "unsafe": False, "unknown": None}[behaviour]
        return JSONResponse(
            {"deployed": ok, "counterexample": ["s0", "s1"]},
            headers={"X-Verdict": normalize(ok).value},
        )

    app = Starlette(routes=[Route("/verify/deploy", deploy, methods=["POST"])])
    app.add_middleware(FailClosedMiddleware, gated_prefixes=("/verify/",), deadline_s=2.0)
    return TestClient(app, raise_server_exceptions=False)


def test_tutorial_branch_table_matches_reality():
    """Each row of the README's before/after table, driven through a real ASGI app."""
    expected = {"safe": 200, "unsafe": 403, "unknown": 403, "raise": 403, "noheader": 403}
    for behaviour, status in expected.items():
        r = _tutorial_client(behaviour).post("/verify/deploy")
        assert r.status_code == status, f"{behaviour} returned {r.status_code}, README says {status}"


def test_tutorial_refusal_preserves_the_counterexample():
    """The README promises the diagnosis survives the refusal."""
    r = _tutorial_client("unsafe").post("/verify/deploy")
    body = r.json()
    assert body["counterexample"] == ["s0", "s1"]
    assert body["refused"] is True


def test_tutorial_missing_header_reason_is_the_one_quoted():
    r = _tutorial_client("noheader").post("/verify/deploy")
    assert r.json()["refusal_reason"] == "gated endpoint returned no machine-checked verdict"


def test_the_quickstart_block_runs_verbatim_and_prints_what_it_claims():
    """The README's quickstart is extracted and executed as written.

    An earlier version was an illustrative fragment calling `app.add_middleware` on an `app` that
    did not exist, so a reader who pasted it got a NameError. A quickstart that does not run is a
    claim the code does not support, so it is now executed here — including the three status codes
    the comments promise.
    """
    import io
    import re
    from contextlib import redirect_stdout
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = re.search(r"##+ 30-second quickstart(.*?)(?=\n##+ )", readme, re.S).group(1)
    code = re.findall(r"```python\n(.*?)```", section, re.S)
    assert len(code) == 1, "the quickstart should be one self-contained block"

    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(code[0], "<README quickstart>", "exec"), {"__name__": "__main__"})
    assert buf.getvalue().split() == ["200", "403", "403"], buf.getvalue()

    # ...and the comments next to those prints say the same thing.
    assert re.findall(r"#\s*(\d{3})\b", code[0]) == ["200", "403", "403"]


def test_no_claim_is_made_about_another_repo_that_this_one_cannot_verify():
    """A line count for a *different* package cannot be checked from here, so it must not be quoted.

    A bulk reconciliation once rewrote the portfolio table's description of `minicheck` using THIS
    repository's line count, so four READMEs confidently stated a wrong number about a package they
    do not contain. Numbers about other repos are now simply absent.
    """
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for line in readme.splitlines():
        if "github.com/nickharris808/" not in line:
            continue
        # The row describing this repo may quote its own numbers; rows about others may not.
        others = [
            m
            for m in re.findall(r"github\.com/nickharris808/([a-z-]+)", line)
            if m != Path(__file__).resolve().parents[1].name
        ]
        if others and re.search(r"~\d+\s+lines|\d+\s+tests", line):
            raise AssertionError(f"unverifiable claim about {others}: {line.strip()}")
