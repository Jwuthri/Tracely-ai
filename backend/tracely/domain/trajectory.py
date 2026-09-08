"""Trajectory model + matcher (the heart of trace-native regression testing).

Canonical types per design 00-canonical-decisions.md §7.2; match modes per `agentevals`
(strict / unordered / subset / superset). A trajectory is built from a trace's span rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# observation `type` -> trajectory StepKind
_KIND = {
    "GENERATION": "llm", "TOOL": "tool", "AGENT": "agent", "SUBAGENT": "subagent",
    "RETRIEVER": "retriever", "CHAIN": "step", "GUARDRAIL": "guardrail",
    "EMBEDDING": "llm", "EVALUATOR": "step", "SPAN": "step", "EVENT": "other",
    "DELEGATE": "delegate", "SKILL": "skill",
}


def step_kind(observation_type: str) -> str:
    return _KIND.get((observation_type or "").upper(), "other")


@dataclass
class TrajectoryStep:
    span_id: str
    parent_span_id: str
    kind: str
    name: str
    level: str
    status: str
    tool_calls: list[str] = field(default_factory=list)
    output: Any = None
    input: Any = None  # tool args / model input — argument predicates and comparison read it


@dataclass
class Trajectory:
    trace_id: str
    agent_run_id: str
    steps: list[TrajectoryStep] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "agent_run_id": self.agent_run_id,
            "steps": [asdict(s) for s in self.steps],
        }


def build_trajectory(spans: list[dict]) -> Trajectory:
    """Build a Trajectory from ordered ClickHouse span rows (ascending start_time)."""
    trace_id = spans[0].get("trace_id", "") if spans else ""
    run_id = ""
    steps: list[TrajectoryStep] = []
    for s in spans:
        is_root = s.get("parent_span_id", "") == "" or s.get("is_app_root")
        if not run_id and is_root:
            run_id = s.get("agent_run_id", "") or trace_id
        level = s.get("level", "DEFAULT")
        steps.append(
            TrajectoryStep(
                span_id=s.get("span_id", ""),
                parent_span_id=s.get("parent_span_id", ""),
                kind=step_kind(s.get("type", "")),
                name=s.get("name", ""),
                level=level,
                status="error" if level == "ERROR" else "ok",
                tool_calls=list(s.get("tool_call_names") or []),
                output=s.get("output"),
                input=s.get("input"),
            )
        )
    return Trajectory(trace_id=trace_id, agent_run_id=run_id or trace_id, steps=steps)


def tool_sequence(traj: Trajectory) -> list[str]:
    """The ordered tool names the agent actually executed (TOOL-type spans)."""
    return [st.name for st in traj.steps if st.kind == "tool"]


def requested_tools(traj: Trajectory) -> list[str]:
    """Tools the model asked for (tool_call_names on any step), de-duplicated in first-seen order.
    A 'silent failure' is a requested tool that was never executed — so this can exceed
    tool_sequence()."""
    seen: list[str] = []
    for st in traj.steps:
        for t in st.tool_calls:
            if t and t not in seen:
                seen.append(t)
    return seen


def required_tools(traj: Trajectory) -> list[str]:
    """Tools a faithful run of this case must execute: every tool this run actually executed, plus
    any tool the model requested but never ran (the silent-failure gap). Keyed off the latter so a
    promoted silent failure asserts the fix really calls the tool — and the source, which didn't,
    FAILs (fail-to-pass)."""
    executed = tool_sequence(traj)
    return executed + [t for t in requested_tools(traj) if t not in executed]


def erroring_steps(traj: Trajectory) -> list[str]:
    return [st.name for st in traj.steps if st.level == "ERROR"]


def split_errors(traj: Trajectory) -> tuple[list[str], list[str]]:
    """(tool_errors, run_errors): erroring TOOL steps (the replayed environment) vs the agent's
    own erroring steps. Lets a case tolerate tool failures while still gating on the run outcome."""
    tool_errs = [st.name for st in traj.steps if st.level == "ERROR" and st.kind == "tool"]
    run_errs = [st.name for st in traj.steps if st.level == "ERROR" and st.kind != "tool"]
    return tool_errs, run_errs


def tools_satisfied(mode: str, produced: list[str], reference: list[str]) -> tuple[bool, list[str], list[str]]:
    """agentevals match modes over the tool sequence. Returns (ok, missing, extra)."""
    missing = [t for t in reference if t not in produced]
    extra = [t for t in produced if t not in reference]
    if mode == "strict":
        ok = produced == reference
    elif mode == "unordered":
        ok = sorted(produced) == sorted(reference)
    elif mode == "subset":
        ok = len(extra) == 0
    else:
        ok = len(missing) == 0
    return ok, missing, extra
