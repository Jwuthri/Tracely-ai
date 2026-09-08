"""Seed the regression → CI-gate demo (the Test → Ship half of Tracely).

Built around a SILENT failure — the model asked for `get_weather` but the agent never executed it,
so it answered without the tool. That's an agent-LOGIC bug, which both gating paths can validate:

  1. the silent production trace is PROMOTED into a regression case (fail-to-pass validated: the
     source FAILs because it never called the required tool)
  2. a CI gate while the bug is still present (still no tool call) -> FAIL
  3. a CI gate after the fix (the agent now calls get_weather) -> PASS
  4. the case is replayed against the fixed trace -> PASS

Because the required tool is the *requested-but-not-executed* one, the hermetic replay path works
too — `tracely replay planner --entrypoint weather_agent:run` PASSes (run calls the tool) while
`...:run_broken` FAILs (it doesn't). The GitHub Action runs exactly that.

    docker compose exec backend python sdk/examples/seed_regression.py
    # or: TRACELY_API=http://localhost:8000 uv run python sdk/examples/seed_regression.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=True)  # provider keys from the repo-root .env


API = os.environ.get("TRACELY_API", "http://localhost:8000")
KEY = os.environ.get("TRACELY_KEY", "tracely_dev_key")
AGENT = os.environ.get("TRACELY_AGENT", "planner")
HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.normpath(os.path.join(HERE, "..", "..", "scripts", "send_test_trace.py"))


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def send_trace(**env_overrides: str) -> str:
    """Run the OTLP sender (deterministic trace ids) and return the trace id it printed."""
    env = {
        **os.environ,
        "TRACELY_API": API,
        "TRACELY_KEY": KEY,
        # Each seeding run emits its OWN traces. Re-sending a deterministic id does not replace
        # the previous send (the events dedup key ends in start_time), so without this a second
        # `make demo` leaves one trace holding both the broken and the fixed run — and the
        # red→green story inverts.
        "TRACELY_FRESH": "1",
        **{k: str(v) for k, v in env_overrides.items()},
    }
    out = subprocess.run(
        [sys.executable, SENDER], env=env, capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"trace_id \(hex\): ([0-9a-f]+)", out)
    if not m:
        raise RuntimeError(f"could not parse trace id from sender output:\n{out}")
    return m.group(1)


def wait_for(trace_id: str, timeout_s: int = 120) -> None:
    """Block until the async pipeline (blob → queue → worker → ClickHouse) has ingested the trace.

    The single-process (`--pool=solo`) worker interleaves ingest with LLM-judge auto-eval, so under
    a burst of demo traces an ingest can queue behind several judge calls — hence the generous
    timeout. (In production the ingest and eval queues should be split / the worker scaled out.)"""
    for _ in range(timeout_s * 2):
        if _req("GET", f"/api/traces/{trace_id}").get("spans"):
            return
        time.sleep(0.5)
    raise TimeoutError(f"trace {trace_id} was never ingested")


def run_gate(git_ref: str, pr: int, env: str = "ci", run_id: str = "") -> dict:
    """Gate the promoted suite. `run_id` scopes candidate pairing to the traces THIS step emitted
    — without it the gate falls back to "newest ci trace with the same input", and a leftover
    trace from an earlier seeder run or a `tracely replay` would be graded instead."""
    body = {"agent": AGENT, "env": env, "git_ref": git_ref, "pr_number": pr}
    if run_id:
        body["run_id"] = run_id
        body["execution_mode"] = "live"
    return _req("POST", "/api/gate", body)


def ci_run(**env_overrides: str) -> tuple[str, str]:
    """Emit one ci trace stamped with a fresh run id; returns `(trace_id, run_id)`."""
    run_id = f"seed-{uuid.uuid4().hex[:12]}"
    tid = send_and_wait(ENV="ci", TRACELY_RUN_ID=run_id, **env_overrides)
    return tid, run_id


def send_and_wait(**env_overrides: str) -> str:
    """Emit a demo trace and block until it's ingested; returns the trace id."""
    tid = send_trace(**env_overrides)
    wait_for(tid)
    return tid


def gate_line(label: str, g: dict) -> None:
    print(
        f"   {label}: gate {g['id'][:8]}  {g['status']}  "
        f"(passed={g['passed']} failed={g['failed']} skipped={g['skipped']})"
    )
    for c in g.get("cases", []):
        d = c.get("detail") or {}
        why = ""
        if d.get("missing_tools"):
            why = f"missing {d['missing_tools']}"
        elif d.get("quality_pass") is False:
            why = f"answer quality {d.get('quality_score')}: {(d.get('quality_reason') or '')[:70]}"
        print(f"        • {c['verdict']:<4} {c['title'][:24]:24} {why}")


def main() -> None:
    print("══ Scenario A — STRUCTURAL bug (silent failure: model requests get_weather, never calls it) ══")
    print("1) production incident → ingest")
    fail_prod = send_trace(SILENT="1")  # silent variant, env=prod
    wait_for(fail_prod)
    print(f"   trace {fail_prod[:16]}…")

    print("2) promote → regression case (asserts the fix must actually call get_weather)")
    case = _req("POST", f"/api/traces/{fail_prod}/promote")
    req = (case.get("assertions") or {}).get("required_tools")
    print(f"   case {case['id'][:8]}  status={case['status']}  required_tools={req}  fail_to_pass={case['fail_to_pass_validated']}")

    print("3) CI gate while the bug is present (no tool call) → expect FAIL")
    _, broken_run = ci_run(SILENT="1")
    gate_line("still broken", run_gate("feat/weather-fix", 41, run_id=broken_run))

    print("4) CI gate after the fix (agent now calls get_weather) → expect PASS")
    fixed_ci, fixed_run = ci_run(FIXED="1")
    gate_line("fixed", run_gate("feat/weather-fix", 41, run_id=fixed_run))

    print("5) replay the case against the fixed trace → expect PASS")
    r = _req("POST", f"/api/cases/{case['id']}/replay", {"candidate_trace_id": fixed_ci})
    print(f"   replay verdict={r['verdict']}")
    # The fix ADDS a get_weather call the original recording never had, so strict recorded
    # replay of the fixed code cannot be served from that recording (it reports INCOMPLETE).
    # Re-record the case from the fixed run: same input, now with the tool's recorded output.
    rc = _req("POST", f"/api/cases/{case['id']}/recapture", {"trace_id": fixed_ci})
    print(f"   re-recorded from the fixed run → case v{rc['case_version']} (artifact {rc['artifact_digest'][:8]})")

    print("\n══ Scenario B — QUALITY bug (HALLUCINATION: get_weather succeeds, but the answer is fabricated) ══")
    print("   A structural check PASSES this (the tool ran!). Only the judge-in-the-gate catches it.")
    print("6) production incident — hallucinated answer → ingest")
    hall_prod = send_trace(HALLUCINATE="1", QUERY_IDX="1")
    wait_for(hall_prod)
    print(f"   trace {hall_prod[:16]}…")

    print("7) promote → regression case (captures the answer-QUALITY failure, not a tool failure)")
    qcase = _req("POST", f"/api/traces/{hall_prod}/promote")
    q = (qcase.get("assertions") or {}).get("quality")
    print(f"   case {qcase['id'][:8]}  status={qcase['status']}  quality={q}  fail_to_pass={qcase['fail_to_pass_validated']}")

    print("8) CI gate while the answer is STILL hallucinated → structural PASS, QUALITY FAIL → gate FAIL")
    # Both cases are in the suite now, so this run must carry BOTH inputs or case A reports
    # INCOMPLETE ("no trace from this run matched"). One run id, two traces.
    hall_run = f"seed-{uuid.uuid4().hex[:12]}"
    send_and_wait(HALLUCINATE="1", QUERY_IDX="1", ENV="ci", TRACELY_RUN_ID=hall_run)
    send_and_wait(FIXED="1", ENV="ci", TRACELY_RUN_ID=hall_run)  # case A is already fixed
    gate_line("still hallucinating", run_gate("feat/answer-fix", 42, run_id=hall_run))

    print("9) CI gate after the answer is fixed (faithful to the tool) → expect PASS")
    fix_run = f"seed-{uuid.uuid4().hex[:12]}"
    faithful_ci = send_and_wait(FIXED="1", QUERY_IDX="1", ENV="ci", TRACELY_RUN_ID=fix_run)
    send_and_wait(FIXED="1", ENV="ci", TRACELY_RUN_ID=fix_run)
    gate_line("answer fixed", run_gate("feat/answer-fix", 42, run_id=fix_run))
    # Recorded-model replay would serve the hallucinated answer back; re-record from the
    # faithful run so the recording carries the fixed answer.
    rc = _req("POST", f"/api/cases/{qcase['id']}/recapture", {"trace_id": faithful_ci})
    print(f"   re-recorded from the fixed run → case v{rc['case_version']}")

    print("\n══ Safety — a gate that matched NO CI traces must NOT be a false green ══")
    gate_line("no coverage", run_gate("test-coverage", 43, env="staging"))

    print("\ndone — Regression cases + CI gates now show TWO red→green stories (structural + quality),")
    print("plus the NO_COVERAGE safety net. Both cases were re-recorded from their fixed runs, so")
    print("strict hermetic replay (recorded tools + recorded model, no keys) tells fix from bug:")
    print(f"   docker compose exec backend sh -c 'cd /app && PYTHONPATH=sdk/examples tracely replay {AGENT} --entrypoint weather_agent:run'        # PASS")
    print(f"   docker compose exec backend sh -c 'cd /app && PYTHONPATH=sdk/examples tracely replay {AGENT} --entrypoint weather_agent:run_broken' # FAIL (missing get_weather)")


if __name__ == "__main__":
    main()
