"""The ONE path that grades a candidate trace against a regression case (W2).

Manual replay (`RegressionService.replay_case`), source validation at promote time, and the CI
gate (`GateService._record_gate_cases`) all call `grade_case`, so a quality-only case cannot pass
in the UI while failing the identical required judge in CI. The judge call is the only I/O; the
verdict itself is `domain.regression.outcome.evaluate_contract`.
"""

from __future__ import annotations

import hashlib
import json

from tracely.config import settings
from tracely.domain.regression.outcome import CaseOutcome, evaluate_contract, execution_evidence
from tracely.domain.trajectory import build_trajectory


def judge_identity(spec: dict) -> dict:
    """Who graded, pinned: the evaluator row, a digest of its config (rubric/model/threshold)
    and the model — so a stored verdict still says what judged it after the evaluator is edited."""
    config = spec.get("config") or {}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "evaluator_id": spec.get("id", ""),
        "config_digest": digest[:12],
        "model": str(config.get("model") or ""),
    }


def mark_verified(case, outcome: CaseOutcome, trace_id: str, by: str) -> bool:
    """Record CANDIDATE-VERIFIED evidence when — and only when — a specific candidate execution
    passed the contract under a completed execution. `by` is "gate:<id>" or "replay". Returns
    whether it was recorded. Never touches `fail_to_pass_validated` (source-failure evidence)."""
    if outcome.verdict != "PASS" or not outcome.execution.complete:
        return False
    from datetime import datetime, timezone

    case.verified_candidate_trace_id = trace_id
    case.verified_case_version = case.version or 1
    case.verified_at = datetime.now(timezone.utc)
    case.verified_by = by[:64]
    return True


def record_case_milestones(session, case, outcome: CaseOutcome, trace_id: str, spans: list[dict], *, verified: bool) -> None:
    """`case_reproduced` (a non-source candidate FAILed under a completed *recorded* execution —
    the failure was reproduced under known conditions) and `candidate_verified`. Best-effort."""
    from tracely.services import milestones

    if trace_id == case.source_trace_id:
        return
    sample = milestones.is_sample(spans)
    integration = milestones.integration_of(spans)
    if outcome.verdict == "FAIL" and outcome.execution.complete and outcome.execution.mode == "recorded":
        milestones.record_own(case.project_id, "case_reproduced", sample=sample, integration=integration)
    if verified:
        milestones.record_own(case.project_id, "candidate_verified", sample=sample, integration=integration)


def grade_case(
    eval_service,
    case,
    spans: list[dict],
    *,
    quality_results: list | None = None,
) -> CaseOutcome:
    """Grade `spans` (the candidate trace) against `case`.

    When the case expects answer-quality judges, they are run here through `eval_service`
    (no persistence) unless the caller already has `quality_results` for these very spans (the
    promote path grades the source once and passes them in). The judges that were *expected* but
    returned nothing surface as UNAVAILABLE required checks — never as a pass."""
    assertions = case.assertions or {}
    expected = list(((assertions.get("quality") or {}).get("score_names")) or [])
    execution = execution_evidence(spans)
    judges: dict[str, dict] = {}
    quality: list[dict] = []
    if expected and execution.complete:  # an incomplete execution has no answer worth judging
        specs = eval_service.quality_specs(case.project_id, expected)
        judges = {s["score_name"]: judge_identity(s) for s in specs}
        results = (
            quality_results
            if quality_results is not None
            else eval_service.grade_trace_quality(case.project_id, spans, specs=specs)
        )
        quality = [
            {"score_name": r.name, "verdict": r.verdict, "value": r.value, "comment": r.comment}
            for r in results
        ]
    return evaluate_contract(
        assertions,
        case.match_mode,
        build_trajectory(spans),
        quality=quality,
        quality_blocks=settings.gate_quality_blocks,
        judges=judges,
        execution=execution,
    )
