"""Fail-to-pass contract evaluation: take a regression case's assertions and a produced
trajectory, return PASS/FAIL with the diagnostic detail dict the UI/CLI surfaces.

Pure: no I/O, no settings, no DB. The match modes come from `agentevals` and live in
`domain/trajectory.py`.
"""

from __future__ import annotations

from typing import Any

import json

from tracely.domain.trajectory import (
    Trajectory,
    erroring_steps,
    split_errors,
    tool_sequence,
    tools_satisfied,
)

# The assertion keys a human may edit (W5). Anything else on the blob is rejected at the API.
EDITABLE_ASSERTIONS = frozenset({
    "required_tools", "forbidden_tools", "match_mode", "no_error", "allow_tool_errors",
    "max_tool_calls", "tool_args",
})
MATCH_MODES = ("superset", "subset", "strict", "unordered")


def _parsed(v):
    """Tool input as recorded is a JSON string (or a dict when built in-process)."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def _lookup(obj, key: str):
    """`a.b.c` path lookup into a parsed tool input; `@observe` tools record `{kwargs: {...}}`
    or the bound args directly, so both shapes are tried."""
    for root in (obj, (obj or {}).get("kwargs") if isinstance(obj, dict) else None):
        cur = root
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = _MISSING
                break
            cur = cur[part]
        if cur is not _MISSING:
            return cur
    return _MISSING


_MISSING = object()


def check_tool_args(predicates: list[dict], traj: Trajectory) -> list[str]:
    """Argument predicates: `[{tool, key, equals}]` — every call to `tool` must carry `key` ==
    `equals` (path keys allowed). Returns the violations, one line each. A tool that was never
    called is NOT a violation here (that is `required_tools`' job)."""
    violations: list[str] = []
    for p in predicates or []:
        tool, key = str(p.get("tool") or ""), str(p.get("key") or "")
        if not tool or not key:
            continue
        for st in traj.steps:
            if st.kind != "tool" or st.name != tool:
                continue
            got = _lookup(_parsed(st.input), key)
            if got is _MISSING:
                violations.append(f"{tool}: argument '{key}' missing")
            elif got != p.get("equals"):
                violations.append(f"{tool}: '{key}' was {json.dumps(got, default=str)[:80]}, expected {json.dumps(p.get('equals'), default=str)[:80]}")
    return violations


def evaluate_case(case, traj: Trajectory) -> tuple[str, dict]:
    """Convenience: pull `assertions` + `match_mode` off an `EvaluationCase` and evaluate.

    Type-loose on `case` so callers don't have to drag in the SQLAlchemy model — anything with
    `.assertions` and `.match_mode` attributes works.
    """
    return evaluate_assertions(case.assertions or {}, case.match_mode, traj)


def evaluate_assertions(
    assertions: dict[str, Any],
    match_mode_default: str,
    traj: Trajectory,
) -> tuple[str, dict]:
    """Run a case's assertions against a produced trajectory.

    `assertions` is the JSON blob from `EvaluationCase.assertions`. `match_mode_default` is the
    case's `match_mode` column, used as a fallback when assertions don't override it (so the
    legacy assertion shape `{required_tools, no_error}` keeps working).
    """
    ref_tools: list[str] = assertions.get("required_tools", [])
    mode: str = assertions.get("match_mode", match_mode_default or "superset")
    produced = tool_sequence(traj)
    tools_ok, missing, extra = tools_satisfied(mode, produced, ref_tools)
    no_error_required = assertions.get("no_error", True)
    # allow_tool_errors: a tool may fail (it's the replayed environment) as long as the agent's
    # own run handles it — so an error-HANDLING fix can pass even though the tool fixture errors.
    allow_tool_errors = assertions.get("allow_tool_errors", False)
    errs = erroring_steps(traj)
    tool_errs, run_errs = split_errors(traj)
    if not no_error_required:
        error_ok = True
    elif allow_tool_errors:
        error_ok = len(run_errs) == 0  # tools may error; the run outcome must be clean
    else:
        error_ok = len(errs) == 0
    forbidden = [t for t in assertions.get("forbidden_tools") or [] if t in produced]
    limits = assertions.get("max_tool_calls") or {}
    over = [
        f"{t}: called {produced.count(t)}×, max {n}"
        for t, n in limits.items()
        if isinstance(n, int) and produced.count(t) > n
    ]
    arg_violations = check_tool_args(assertions.get("tool_args") or [], traj)
    passed = tools_ok and error_ok and not forbidden and not over and not arg_violations
    detail = {
        "passed": passed,
        "tools_ok": tools_ok,
        "error_ok": error_ok,
        "forbidden_hit": forbidden,
        "count_violations": over,
        "arg_violations": arg_violations,
        "match_mode": mode,
        "allow_tool_errors": allow_tool_errors,
        "required_tools": ref_tools,
        "produced_tools": produced,
        "missing_tools": missing,
        "extra_tools": extra,
        "erroring_steps": errs,
        "tool_errors": tool_errs,
        "run_errors": run_errs,
    }
    return ("PASS" if passed else "FAIL"), detail
