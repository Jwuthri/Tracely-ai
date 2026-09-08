"""Promote a (failing) trace into a regression `EvaluationCase`, and replay cases.

A regression case derived from a failing trace asserts: replaying the same input must NOT
reproduce the failure AND must still call the required tools. That gives the FAIL-TO-PASS
contract for free — it FAILS on the broken run and PASSES once fixed.

The service composes:
- `TraceReader` (ClickHouse `events` reads)
- `ScoreWriter` (the regression verdict score row)
- `BlobStore` module functions (fixture bundle upload)
- `grade_case` from `services.case_grading` — the ONE contract shared with the CI gate
- `FixtureBundle.capture` from `domain.regression.fixtures`
- `root_span` / `input_digest` from `domain.traces.spans`
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracely.config import settings
from tracely.domain.regression.artifact import CaseArtifact, artifact_key
from tracely.domain.regression.compare import align_steps
from tracely.domain.regression.contract import EDITABLE_ASSERTIONS, MATCH_MODES, evaluate_assertions
from tracely.domain.regression.fixtures import FixtureBundle
from tracely.domain.trajectory import (
    Trajectory,
    build_trajectory,
    required_tools,
    split_errors,
)
from tracely.domain.traces.spans import input_digest, root_span
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.clickhouse.score_writer import ScoreWriter
from tracely.infrastructure.clickhouse.trace_reader import TraceReader
from tracely.infrastructure.db.models import (
    CaseReplay,
    EvaluationCase,
    EvaluationSuite,
    EvaluationSuiteCase,
)
from tracely.services.case_grading import grade_case, judge_identity, mark_verified, record_case_milestones
from tracely.services.evaluation_service import EvaluationService


class NotFound(Exception):
    pass


class RegressionService:
    """Use-case orchestrator: promote/replay regression cases."""

    def __init__(
        self,
        session: Session,
        trace_reader: TraceReader | None = None,
        score_writer: ScoreWriter | None = None,
        eval_service: EvaluationService | None = None,
    ) -> None:
        self.session = session
        self.trace_reader = trace_reader or TraceReader()
        self._score_writer = score_writer
        # Grades answer quality on the source trace so a hallucination (clean trace, bad answer)
        # becomes a promotable, gate-able case — not just structural tool/error failures.
        # `score_writer` is forwarded as-is (possibly None) rather than via the property below, so
        # constructing the service stays connection-free — see the note on `score_writer`.
        self.eval_service = eval_service or EvaluationService(
            trace_reader=self.trace_reader, score_writer=score_writer
        )

    # Lazy for the same reason as EvaluationService.score_writer: reading `trace_reader.client` is
    # what opens the ClickHouse socket, so doing it in __init__ made construction a connection.
    @property
    def score_writer(self) -> ScoreWriter:
        if self._score_writer is None:
            self._score_writer = ScoreWriter(self.trace_reader.client)
        return self._score_writer

    @score_writer.setter
    def score_writer(self, writer: ScoreWriter) -> None:
        self._score_writer = writer

    # ── reads (used by callers that just need the trace) ──────────────────────

    def read_spans(self, project_id: str, trace_id: str) -> list[dict]:
        return self.trace_reader.read_spans(project_id, trace_id)

    # ── core operations ───────────────────────────────────────────────────────

    def promote_trace(
        self, project_id: str, trace_id: str, title: str | None = None
    ) -> EvaluationCase:
        """Turn a (failing) trace into a regression `EvaluationCase`. Idempotent on
        `(project_id, agent_id, input_digest)`."""
        spans = self.trace_reader.read_spans(project_id, trace_id)
        if not spans:
            raise NotFound("trace not found")
        traj = build_trajectory(spans)
        root = root_span(spans)
        agent_id = root.get("agent_id") or next(
            (s.get("agent_id") for s in spans if s.get("agent_id")), ""
        )
        digest = input_digest(spans)

        existing = self._existing_case(project_id, agent_id, digest)
        if existing:
            return existing  # idempotent

        assertions = self._build_assertions(traj)
        # Answer-quality judges that the SOURCE trace failed → the case guards against them too,
        # so a hallucination with a structurally-clean trace is still promotable + gate-able.
        quality_results = self._grade_source_quality(project_id, spans)
        quality_failed = [r.name for r in quality_results if r.verdict == "FAIL"]
        if quality_failed:
            assertions["quality"] = {"score_names": quality_failed}
        bundle = FixtureBundle.capture(spans)
        fixture_key = self._store_fixtures(project_id, digest, bundle)
        # One logical promote = one transaction. The helpers FLUSH (so later steps see the rows) but
        # never commit mid-way; a crash anywhere rolls the whole thing back instead of leaving a
        # half-promoted case (case without suite link, or a PROMOTED case with no validation replay).
        # Blob keys are deterministic, so a retried promote overwrites its own orphans.
        case = self._create_case(
            project_id=project_id, agent_id=agent_id, trace_id=trace_id, root=root,
            digest=digest, title=title, assertions=assertions, fixture_key=fixture_key,
            trajectory_json=traj.to_json(),
        )
        self._attach_to_regression_suite(project_id, agent_id, case)
        recorded = self._record_fail_to_pass(case, spans, trace_id, quality_results)
        # The durable artifact is written BEFORE the commit: if the blob store is down the
        # promote fails whole rather than leaving a case that will not survive retention.
        self.snapshot_artifact(case, input_text=self._root_input(spans), fixtures=bundle.to_dict())
        self.session.commit()
        # ClickHouse write is external (non-transactional) → do it only after the PG commit succeeds.
        self.score_writer.write_regression_verdict(case, trace_id, recorded)
        if case.fail_to_pass_validated:
            from tracely.services import milestones

            milestones.record_own(
                project_id, "source_failure_confirmed",
                sample=milestones.is_sample(spans), integration=milestones.integration_of(spans),
            )
        return case

    def replay_case(
        self, project_id: str, case_id: str, candidate_trace_id: str
    ) -> CaseReplay:
        """Re-evaluate a case against a candidate trace and record a `CaseReplay` row."""
        case = self.session.get(EvaluationCase, case_id)
        if not case or case.project_id != project_id:
            raise NotFound("case not found")
        spans = self.trace_reader.read_spans(project_id, candidate_trace_id)
        if not spans:
            raise NotFound("candidate trace not found")
        outcome = grade_case(self.eval_service, case, spans)
        # A manual replay of the SOURCE trace is a re-check of source failure, not a candidate.
        verified = candidate_trace_id != case.source_trace_id and mark_verified(case, outcome, candidate_trace_id, "replay")
        replay = CaseReplay(
            id=str(uuid.uuid4()), case_id=case.id, candidate_trace_id=candidate_trace_id,
            verdict=outcome.verdict,
            detail={**outcome.detail, "case_version": case.version or 1, "artifact_digest": case.artifact_digest or ""},
        )
        self.session.add(replay)
        self.session.commit()
        self.score_writer.write_regression_verdict(case, candidate_trace_id, outcome.verdict)
        record_case_milestones(self.session, case, outcome, candidate_trace_id, spans, verified=bool(verified))
        return replay

    # ── pure-ish helpers (no I/O beyond the session/trace_reader/score_writer) ────

    def _grade_source_quality(self, project_id: str, spans: list[dict]) -> list:
        """The answer-quality judge results for the SOURCE trace. The ones it FAILed become the
        case's expected judges; the gate and manual replay re-check them on every candidate.
        Empty when no judge is configured / no LLM key — the case then stays a structural-only
        regression (old behavior)."""
        return list(self.eval_service.grade_trace_quality(project_id, spans))

    @staticmethod
    def _build_assertions(traj: Trajectory) -> dict:
        """What the fixed agent must do, derived from the trace that broke.

        Deliberately tool NAMES, never tool ARGUMENTS. The reference here is a production failure,
        not a golden run: asserting the recorded args would assert the bug, and an agent fixed to
        pass the right ones would fail this case for ever. A trace whose only fault was bad
        arguments therefore yields a case its own source PASSES — `_record_fail_to_pass` leaves
        that DRAFT and unvalidated, which is the honest answer ("this one can't discriminate")
        rather than a gate that is green on a bug or red on a fix. Arg-level cases need a
        human-authored expectation on the assertions blob; see migration 0029.
        """
        # Required tools = everything the agent executed PLUS any tool the model requested but
        # never ran (the silent-failure gap). The case asserts the fixed agent actually calls it.
        ref_tools = required_tools(traj)
        # If the source failed because a tool errored AND the agent itself errored, the
        # regression is "handle the tool error gracefully" — tolerate tool errors and gate on
        # the run outcome.
        src_tool_errs, src_run_errs = split_errors(traj)
        allow_tool_errors = bool(src_tool_errs and src_run_errs)
        return {
            "no_error": True,
            "required_tools": ref_tools,
            "match_mode": "superset",
            "allow_tool_errors": allow_tool_errors,
        }

    @staticmethod
    def _store_fixtures(project_id: str, digest: str, bundle: FixtureBundle) -> str:
        key = f"{settings.s3_event_prefix}fixtures/{project_id}/{digest}.json"
        blobstore.put_blob(key, bundle.encode(), "application/json")
        return key

    @staticmethod
    def _root_input(spans: list[dict]) -> str:
        """The executable input: the root span's, else the first span that has one."""
        if not spans:
            return ""
        r = root_span(spans)
        if r.get("input"):
            return str(r["input"])
        return next((str(s["input"]) for s in spans if s.get("input")), "")

    # ── the failure-to-fix workspace (W5) ─────────────────────────────────────

    def update_expectations(self, project_id: str, case_id: str, patch: dict) -> EvaluationCase:
        """Edit what the fixed agent must do. Whitelisted keys only; bumps the case version,
        re-snapshots the artifact for the new version (so a gate result pins the contract it
        was graded against) and re-checks that the SOURCE still fails the contract when its
        spans are available — a case whose source passes cannot discriminate and goes DRAFT.
        Candidate verification recorded at the old version is left in place; the UI reads it as
        stale by version."""
        case = self.session.get(EvaluationCase, case_id)
        if not case or case.project_id != project_id:
            raise NotFound("case not found")
        bad = set(patch) - EDITABLE_ASSERTIONS
        if bad:
            raise ValueError(f"unknown assertion(s): {', '.join(sorted(bad))}")
        if "match_mode" in patch and patch["match_mode"] not in MATCH_MODES:
            raise ValueError(f"match_mode must be one of {', '.join(MATCH_MODES)}")
        for key in ("required_tools", "forbidden_tools"):
            if key in patch and not (isinstance(patch[key], list) and all(isinstance(t, str) for t in patch[key])):
                raise ValueError(f"{key} must be a list of tool names")
        if "max_tool_calls" in patch and not (
            isinstance(patch["max_tool_calls"], dict)
            and all(isinstance(n, int) and n >= 0 for n in patch["max_tool_calls"].values())
        ):
            raise ValueError("max_tool_calls must map tool name → non-negative integer")
        if "tool_args" in patch and not (
            isinstance(patch["tool_args"], list)
            and all(isinstance(p, dict) and p.get("tool") and p.get("key") for p in patch["tool_args"])
        ):
            raise ValueError("tool_args must be a list of {tool, key, equals}")
        art, _ = self.load_artifact(case)
        spans = self.read_spans(project_id, case.source_trace_id) if case.source_trace_id else []
        if art is None and not spans:
            raise ValueError("this case has no durable artifact and its source trace is gone — recapture it first")

        case.assertions = {**(case.assertions or {}), **patch}
        if "match_mode" in patch:
            case.match_mode = patch["match_mode"]
        case.version = (case.version or 1) + 1
        input_text = art.input_text if art else self._root_input(spans)
        fixtures = art.fixtures if art else FixtureBundle.capture(spans).to_dict()
        self.snapshot_artifact(case, input_text=input_text, fixtures=fixtures)
        if spans:
            quality = self._grade_source_quality(project_id, spans) if (case.assertions.get("quality")) else []
            self._record_fail_to_pass(case, spans, case.source_trace_id, quality)
        self.session.commit()
        return case

    def compare(self, project_id: str, case_id: str, candidate_trace_id: str) -> dict:
        """Original vs candidate: aligned relevant steps + the candidate's structural verdict
        (no judge call — the persisted replay carries the full checked outcome)."""
        case = self.session.get(EvaluationCase, case_id)
        if not case or case.project_id != project_id:
            raise NotFound("case not found")
        spans = self.read_spans(project_id, candidate_trace_id)
        if not spans:
            raise NotFound("candidate trace not found")
        cand = build_trajectory(spans)
        ref_steps = (case.reference_trajectory or {}).get("steps") or []
        aligned = align_steps(ref_steps, [s.__dict__ for s in cand.steps])
        verdict, detail = evaluate_assertions(case.assertions or {}, case.match_mode, cand)
        answer = next((str(s.get("output") or "") for s in spans if s.get("parent_span_id", "") == "" or s.get("is_app_root")), "")
        ref_answer = next((str(s.get("output") or "") for s in ref_steps if s.get("parent_span_id", "") == ""), "")
        return {
            **aligned,
            "structural_verdict": verdict,
            "structural": {k: detail[k] for k in ("missing_tools", "extra_tools", "forbidden_hit", "count_violations", "arg_violations", "run_errors", "tool_errors") if k in detail},
            "answers": {"reference": ref_answer[:2000], "candidate": answer[:2000]},
        }

    def candidates(self, project_id: str, case_id: str, limit: int = 20) -> list[dict]:
        """Recent runs of the case's agent with the SAME input — exact compatible candidates,
        newest first, each marked source / already-replayed verdict / run id."""
        case = self.session.get(EvaluationCase, case_id)
        if not case or case.project_id != project_id:
            raise NotFound("case not found")
        art, _ = self.load_artifact(case)
        text = art.input_text if art else self._root_input(self.read_spans(project_id, case.source_trace_id))
        if not text:
            return []
        replayed = {
            r.candidate_trace_id: r.verdict
            for r in self.session.execute(
                select(CaseReplay).where(CaseReplay.case_id == case.id).order_by(CaseReplay.created_at)
            ).scalars()
        }
        out = []
        for row in self.trace_reader.traces_with_input(project_id, case.agent_id, text, limit):
            out.append({
                **row,
                "is_source": row["trace_id"] == case.source_trace_id,
                "replay_verdict": replayed.get(row["trace_id"]),
            })
        return out

    # ── durable artifacts (W3) ────────────────────────────────────────────────

    def snapshot_artifact(
        self, case: EvaluationCase, *, input_text: str, fixtures: dict
    ) -> CaseArtifact:
        """Write the artifact for the case's CURRENT version and pin its key + digest on the row
        (flushed, not committed — the caller owns the transaction). Raises ValueError on an empty
        input: an artifact that cannot execute is never written."""
        expected = list(((case.assertions or {}).get("quality") or {}).get("score_names") or [])
        evaluators = (
            {s["score_name"]: judge_identity(s) for s in self.eval_service.quality_specs(case.project_id, expected)}
            if expected
            else {}
        )
        art = CaseArtifact.build(
            case_id=case.id,
            case_version=case.version or 1,
            input_text=input_text,
            fixtures=fixtures,
            assertions=case.assertions or {},
            match_mode=case.match_mode,
            evaluators=evaluators,
            provenance={
                "project_id": case.project_id,
                "agent_id": case.agent_id,
                "agent_version_first_failed": case.agent_version_first_failed,
                "source_trace_id": case.source_trace_id,
                "source_span_id": case.source_span_id,
                "input_digest": case.input_digest,
            },
        )
        key = artifact_key(settings.s3_event_prefix, case.project_id, case.id, art.case_version)
        blobstore.put_blob(key, art.encode(), "application/json")
        case.artifact_s3_key, case.artifact_digest = key, art.digest()
        self.session.flush()
        return art

    def load_artifact(self, case: EvaluationCase) -> tuple[CaseArtifact | None, str | None]:
        """`(artifact, None)` or `(None, why)` — a missing or corrupt artifact is an explicit
        incomplete state for the case, never an empty input."""
        if not case.artifact_s3_key:
            return None, "no durable artifact for this case yet (run the backfill, or recapture from a trace)"
        try:
            return CaseArtifact.decode(blobstore.get_blob(case.artifact_s3_key)), None
        except Exception as exc:
            return None, f"case artifact {case.artifact_s3_key} could not be loaded: {exc}"

    def backfill_artifacts(self, project_id: str | None = None) -> dict:
        """Snapshot every case that has no artifact yet, from whatever still exists: the source
        trace's spans (input) plus its stored fixture bundle. A case whose source has already
        expired is left unrecoverable — reported, not invented — and needs `recapture`."""
        from tracely.infrastructure.db.repositories import cases_without_artifact

        done: list[str] = []
        unrecoverable: dict[str, str] = {}
        for case in cases_without_artifact(self.session, project_id):
            spans = self.trace_reader.read_spans(case.project_id, case.source_trace_id) if case.source_trace_id else []
            input_text = self._root_input(spans)
            if not input_text:
                unrecoverable[case.id] = "source trace no longer available — recapture from a fresh trace with the same input"
                continue
            try:
                fixtures = FixtureBundle.decode(blobstore.get_blob(case.fixture_bundle_s3_key)) if case.fixture_bundle_s3_key else FixtureBundle.capture(spans).to_dict()
            except Exception:  # bundle unreadable but the source is still here: re-capture it
                fixtures = FixtureBundle.capture(spans).to_dict()
            try:
                self.snapshot_artifact(case, input_text=input_text, fixtures=fixtures)
                self.session.commit()
                done.append(case.id)
            except Exception as exc:
                self.session.rollback()
                unrecoverable[case.id] = f"snapshot failed: {exc}"
        return {"snapshotted": done, "unrecoverable": unrecoverable}

    def recapture(self, project_id: str, case_id: str, trace_id: str) -> CaseArtifact:
        """Rebuild a case's artifact from a fresh trace of the SAME input (its `input_digest` must
        match) — the recovery path for a case whose source expired before it was snapshotted.
        Bumps the case version: the recording changed, so results pinned to the old one stay
        theirs."""
        case = self.session.get(EvaluationCase, case_id)
        if not case or case.project_id != project_id:
            raise NotFound("case not found")
        spans = self.trace_reader.read_spans(project_id, trace_id)
        if not spans:
            raise NotFound("trace not found")
        if input_digest(spans) != case.input_digest:
            raise ValueError("trace input does not match this case's input digest")
        bundle = FixtureBundle.capture(spans)
        case.fixture_bundle_s3_key = self._store_fixtures(project_id, case.input_digest, bundle)
        case.version = (case.version or 1) + 1
        art = self.snapshot_artifact(case, input_text=self._root_input(spans), fixtures=bundle.to_dict())
        self.session.commit()
        return art

    def _existing_case(
        self, project_id: str, agent_id: str, digest: str
    ) -> Optional[EvaluationCase]:
        return self.session.execute(
            select(EvaluationCase).where(
                EvaluationCase.project_id == project_id,
                EvaluationCase.agent_id == agent_id,
                EvaluationCase.input_digest == digest,
            )
        ).scalar_one_or_none()

    def _create_case(
        self,
        *,
        project_id: str,
        agent_id: str,
        trace_id: str,
        root: dict,
        digest: str,
        title: str | None,
        assertions: dict,
        fixture_key: str,
        trajectory_json: dict,
    ) -> EvaluationCase:
        case = EvaluationCase(
            id=str(uuid.uuid4()), project_id=project_id, agent_id=agent_id, level="AGENT_RUN",
            title=title or root.get("name", "") or "case", input_digest=digest, status="DRAFT",
            origin="MANUAL", source_trace_id=trace_id, source_span_id=root.get("span_id", ""),
            agent_version_first_failed=root.get("agent_version_id") or None,
            fixture_bundle_s3_key=fixture_key, reference_trajectory=trajectory_json,
            assertions=assertions, match_mode="superset",
            fail_to_pass_validated=False, version=1, created_by="ui",
        )
        self.session.add(case)
        self.session.flush()
        return case

    def _attach_to_regression_suite(
        self, project_id: str, agent_id: str, case: EvaluationCase
    ) -> None:
        suite = self.session.execute(
            select(EvaluationSuite).where(
                EvaluationSuite.project_id == project_id,
                EvaluationSuite.agent_id == agent_id,
                EvaluationSuite.slug == "regressions",
            )
        ).scalar_one_or_none()
        if not suite:
            suite = EvaluationSuite(
                id=str(uuid.uuid4()), project_id=project_id, agent_id=agent_id,
                slug="regressions", name="Regressions", kind="REGRESSION",
            )
            self.session.add(suite)
            self.session.flush()
        self.session.add(EvaluationSuiteCase(suite_id=suite.id, case_id=case.id))
        self.session.flush()

    def _record_fail_to_pass(
        self,
        case: EvaluationCase,
        spans: list[dict],
        trace_id: str,
        quality_results: list | None = None,
    ) -> str:
        """The source (failing) trace must currently FAIL the case for it to be PROMOTED — either
        structurally (the tool/error contract) OR on a required answer-quality judge (a
        hallucination whose trace is structurally clean). Graded through the SAME contract the
        gate applies, so a judge the gate would treat as advisory cannot promote a case here.
        Otherwise the case is a non-discriminating no-op and stays DRAFT.

        What this establishes is SOURCE-FAILURE evidence only (`detail.evidence`), never that any
        candidate passed. Stages the verdict replay + status into the session (no commit — the
        caller commits the whole promote as one transaction) and returns the recorded verdict so
        the caller can write the external ClickHouse score row AFTER the commit succeeds."""
        outcome = grade_case(self.eval_service, case, spans, quality_results=quality_results or [])
        recorded = outcome.verdict
        case.fail_to_pass_validated = recorded == "FAIL"
        case.status = "PROMOTED" if case.fail_to_pass_validated else "DRAFT"
        self.session.add(CaseReplay(
            id=str(uuid.uuid4()), case_id=case.id, candidate_trace_id=trace_id,
            verdict=recorded, detail={**outcome.detail, "validation": True, "evidence": "source_failure"},
        ))
        self.session.flush()
        return recorded
