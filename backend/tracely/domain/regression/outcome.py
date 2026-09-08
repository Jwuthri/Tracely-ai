"""One evaluation contract for a regression case (W2): the same case and candidate mean the same
thing in the UI's manual replay, the CLI, and the CI gate.

A case outcome is a list of named CHECKS, each required or advisory, each PASS / FAIL /
UNAVAILABLE — plus EXECUTION evidence (which mode actually ran, whether it completed). The
verdict is derived from those, never from a bare "did any check say FAIL":

    FAIL        a required check failed (a behavioural failure)
    INCOMPLETE  no required check failed, but one could not run (judge unavailable, fixtures
                missing, execution error) — the candidate has NOT been shown to pass
    PASS        every required check ran and passed, under a completed execution
    SKIP        the case was not exercised at all (the gate's coverage problem, see `gate_status`)

Pure: no I/O, no settings. Services gather the judge results + spans and call in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tracely.domain.regression.contract import evaluate_assertions
from tracely.domain.traces.spans import root_span
from tracely.domain.trajectory import Trajectory

PASS, FAIL, INCOMPLETE, SKIP, UNAVAILABLE = "PASS", "FAIL", "INCOMPLETE", "SKIP", "UNAVAILABLE"
# Bumped when the aggregation rules below change, so a stored result says which policy made it.
POLICY = "required-checks/v1"

# The gate statuses, worst last. NO_COVERAGE (suite exercised nothing) and INCOMPLETE (a case
# could not be fully checked) both block; FAIL names an actual regression.
GATE_SEVERITY = {"PASS": 0, "NO_COVERAGE": 1, "INCOMPLETE": 2, "FAIL": 3}


@dataclass(frozen=True)
class Check:
    name: str  # "tools" | "no_error" | "quality:<score_name>"
    required: bool
    status: str  # PASS | FAIL | UNAVAILABLE
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "required": self.required, "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class Execution:
    """What actually ran to produce the candidate trace. Read off the trace by
    `execution_evidence`; `Execution()` = nothing known (a trace that didn't come from replay)."""

    mode: str = "unknown"  # recorded | lenient | live | unknown
    problem: str = ""  # non-empty = the run did not complete under its declared mode
    diverged: list[str] = field(default_factory=list)  # spans served despite a different input

    @property
    def complete(self) -> bool:
        return not self.problem

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "complete": self.complete,
            "problem": self.problem,
            "diverged": list(self.diverged),
        }


@dataclass(frozen=True)
class CaseOutcome:
    verdict: str
    checks: list[Check]
    execution: Execution
    detail: dict  # the full diagnostic payload stored on CaseReplay / GateCase


def execution_evidence(spans: list[dict]) -> Execution:
    """Execution facts the SDK stamps on a replayed trace: the root's `tracely.replay.mode`, a
    root error the CLI prefixed `replay error:` (fixtures unavailable, a strict miss), and any
    span served despite an input divergence."""
    if not spans:
        return Execution()
    root = root_span(spans)
    meta = root.get("metadata") or {}
    mode = str(meta.get("tracely.replay.mode") or "unknown")
    msg = str(root.get("status_message") or "")
    problem = msg if root.get("level") == "ERROR" and msg.startswith("replay error:") else ""
    diverged = [
        str(s.get("name") or "")
        for s in spans
        if (s.get("metadata") or {}).get("tracely.replay.divergence")
    ]
    return Execution(mode=mode, problem=problem, diverged=diverged)


def aggregate(checks: list[Check], execution: Execution) -> str:
    """The verdict policy. Order matters: a behavioural FAIL outranks an execution problem so the
    two stay distinguishable even when both block; an unavailable REQUIRED check can never be
    read as a pass; advisory checks never decide."""
    required = [c for c in checks if c.required]
    if any(c.status == FAIL for c in required):
        return FAIL
    if not execution.complete or any(c.status == UNAVAILABLE for c in required):
        return INCOMPLETE
    return PASS


def evaluate_contract(
    assertions: dict[str, Any],
    match_mode: str,
    traj: Trajectory,
    *,
    quality: list[dict] | None = None,
    quality_blocks: bool = True,
    judges: dict[str, dict] | None = None,
    execution: Execution | None = None,
) -> CaseOutcome:
    """Grade a candidate trajectory against a case: structural assertions + the answer-quality
    judges the case expects (`assertions["quality"]["score_names"]`).

    `quality` is what the service got back from running those judges — `{score_name, verdict,
    value, comment}` each. An expected judge with NO result is UNAVAILABLE: an empty or partial
    list never implies the answer was checked. `quality_blocks=False` makes the quality checks
    advisory (visible, never deciding). `judges` is the judge identity/config captured at grading
    time, stored beside the result so a historical verdict stays explainable after the evaluator
    changes. `execution` is the candidate trace's execution evidence; a run that did not complete
    makes the structural checks UNAVAILABLE (there is no trajectory worth asserting on)."""
    execution = execution or Execution()
    structural, detail = evaluate_assertions(assertions, match_mode, traj)
    checks: list[Check] = []
    if not execution.complete:
        why = f"not evaluated: {execution.problem}"
        checks.append(Check("tools", True, UNAVAILABLE, why))
        if assertions.get("no_error", True):
            checks.append(Check("no_error", True, UNAVAILABLE, why))
    else:
        tools_reason = ""
        if detail["missing_tools"]:
            tools_reason = "missing tools: " + ", ".join(detail["missing_tools"])
        elif not detail["tools_ok"]:
            tools_reason = f"tool sequence mismatch (mode={detail['match_mode']})"
        checks.append(Check("tools", True, PASS if detail["tools_ok"] else FAIL, tools_reason))
        if assertions.get("no_error", True):
            err_reason = ""
            if detail["run_errors"]:
                err_reason = "run failed: " + ", ".join(detail["run_errors"])
            elif not detail["error_ok"]:
                err_reason = "errors: " + ", ".join(detail["erroring_steps"])
            checks.append(Check("no_error", True, PASS if detail["error_ok"] else FAIL, err_reason))
        if assertions.get("forbidden_tools"):
            hit = detail.get("forbidden_hit") or []
            checks.append(Check("forbidden_tools", True, FAIL if hit else PASS, ("called: " + ", ".join(hit)) if hit else ""))
        if assertions.get("max_tool_calls"):
            over = detail.get("count_violations") or []
            checks.append(Check("call_counts", True, FAIL if over else PASS, "; ".join(over)))
        if assertions.get("tool_args"):
            bad = detail.get("arg_violations") or []
            checks.append(Check("tool_args", True, FAIL if bad else PASS, "; ".join(bad)))

    expected = list(((assertions.get("quality") or {}).get("score_names")) or [])
    got = {q.get("score_name"): q for q in quality or []}
    for name in expected:
        q = got.get(name)
        if q is None:
            checks.append(
                Check(
                    f"quality:{name}",
                    quality_blocks,
                    UNAVAILABLE,
                    "judge returned no result (no LLM key for this workspace, judge disabled or "
                    "deleted, or it errored)",
                )
            )
        elif q.get("verdict") == FAIL:
            checks.append(Check(f"quality:{name}", quality_blocks, FAIL, str(q.get("comment") or "")))
        elif q.get("verdict") == PASS:
            checks.append(Check(f"quality:{name}", quality_blocks, PASS, str(q.get("comment") or "")))
        else:
            checks.append(Check(f"quality:{name}", quality_blocks, UNAVAILABLE, "judge gave no verdict"))

    verdict = aggregate(checks, execution)
    quality_checks = [c for c in checks if c.name.startswith("quality:")]
    q_failed = [c for c in quality_checks if c.status == FAIL]
    q_worst = q_failed[0] if q_failed else (quality_checks[0] if quality_checks else None)
    q_result = got.get(q_worst.name.removeprefix("quality:")) if q_worst else None
    merged = {
        **detail,
        "structural_verdict": structural,
        "checks": [c.to_dict() for c in checks],
        "execution": execution.to_dict(),
        "policy": POLICY,
        "judges": judges or {},
        # Legacy flat keys the CLI/UI already read; kept so an old reader still explains the row.
        "quality_checked": bool(quality_checks) and all(c.status != UNAVAILABLE for c in quality_checks),
        "quality_pass": (not q_failed) if quality_checks else None,
        "quality_score": q_result.get("value") if q_result else None,
        "quality_score_name": q_worst.name.removeprefix("quality:") if q_worst else None,
        "quality_reason": q_result.get("comment") if q_result else (q_worst.reason if q_worst else None),
    }
    return CaseOutcome(verdict=verdict, checks=checks, execution=execution, detail=merged)


def gate_status(
    passed: int,
    failed: int,
    skipped: int,
    incomplete: int,
    total: int,
    *,
    require_full_coverage: bool = False,
) -> str:
    """Aggregate case verdicts into the gate status — the policy table, in order:

        any FAIL                          → FAIL          a known regression came back
        no cases at all                   → PASS          nothing to protect yet (vacuous; the UI
                                                          must not present this as a first test)
        any INCOMPLETE                    → INCOMPLETE    a case could not be fully checked
        nothing passed (all SKIP)         → NO_COVERAGE   the run exercised none of the suite
        some SKIP + full coverage required → NO_COVERAGE
        otherwise                         → PASS
    """
    if failed > 0:
        return FAIL
    if total == 0:
        return PASS
    if incomplete > 0:
        return INCOMPLETE
    if passed == 0:
        return "NO_COVERAGE"
    if skipped > 0 and require_full_coverage:
        return "NO_COVERAGE"
    return PASS


def worst_gate_status(*statuses: str) -> str:
    return max(statuses, key=lambda s: GATE_SEVERITY.get(s, GATE_SEVERITY[FAIL]))
