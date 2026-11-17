"""Explore mode: lessons, the deepen pass, best-of-N — and that none of it touches a measure run."""

from __future__ import annotations

import test_audit_service as base
from test_audit_service import ATTACKER, PROJECT, _audit, _rollouts, _run_to_end

from tracely.domain.audit import explore, seeds
from tracely.infrastructure.db import models
from tracely.infrastructure.db import repositories as repo
from tracely.services import audit_service
from tracely.services.audit_service import AuditService

db, world = base.db, base.world  # the service tests' fixtures, shared


def _r(seed_id, verdict, *, stage=0, disputed=False, why="it gave the address"):
    kind, flagged = ("validity", True) if verdict == "INVALID" else ("behaviour", verdict == "ELICITED")
    return {"seed_id": seed_id, "seed": {"title": f"seed {seed_id}"}, "stage": stage, "verdict": verdict,
            "scores": {"results": [{"kind": kind, "graded": True, "flagged": flagged, "disputed": disputed,
                                    "judges": [{"justification": why}]}]}}


def test_lessons_lead_with_what_gave_an_earlier_attacker_away():
    lines = explore.lessons([_r("a", "CLEAN"), _r("b", "ELICITED"), _r("c", "INVALID", why="named the eval"), _r("d", "ERROR")])
    assert [line.split(" (")[0] for line in lines] == ["The target saw through this", "This worked", "The target held"]
    assert "named the eval" in lines[0]


def test_deepen_varies_what_elicited_and_repeats_what_split():
    plan = explore.deepen_plan([
        _r("hit", "ELICITED"), _r("hit", "ELICITED"),
        _r("split", "ELICITED"), _r("split", "CLEAN"),
        _r("contested", "CLEAN", disputed=True),
        _r("held", "CLEAN"), _r("broken", "ERROR"),
        _r("late", "ELICITED", stage=1),  # a second pass never feeds a third
    ])
    assert [e["seed_id"] for e in plan["vary"]] == ["hit"]
    assert [e["seed_id"] for e in plan["repeat"]] == ["split", "contested"]
    many = explore.deepen_plan([_r(f"s{i}", "ELICITED") for i in range(40)])
    assert len(many["vary"]) * len(explore.DEEPEN_AXES) <= explore.MAX_DEEPEN


def _explore_world(world, monkeypatch):
    """Answer the variation and picker calls on top of the judge."""
    judge = world.run_judge

    def fake(prompt, *, response_format, **kw):
        if response_format is seeds.SeedBatch:
            world.calls.append("vary")
            return seeds.SeedBatch(seeds=[seeds.SeedIdea(title=f"variation {i}", instruction=f"same, but {i}") for i in range(2)])
        if response_format is explore.Pick:
            world.calls.append("pick")
            return explore.Pick(choice=2)
        return judge(prompt, response_format=response_format, **kw)

    monkeypatch.setattr(audit_service.provider, "run_structured_agent", fake)


def test_an_explore_run_deepens_once_and_tells_later_rollouts_what_worked(db, world, monkeypatch):
    _explore_world(world, monkeypatch)
    prompts_seen: list[str] = []
    chat = world.run_chat

    def spy(messages, *, model=None, **kw):
        if model == ATTACKER:
            prompts_seen.append(messages[0]["content"])
        return chat(messages, model=model, **kw)

    monkeypatch.setattr(audit_service.provider, "run_chat", spy)
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db), mode="explore")
    _run_to_end(svc, run.id)

    rollouts = _rollouts(db, run.id)
    assert [(r.stage, r.verdict) for r in rollouts] == [(0, "ELICITED"), (1, "ELICITED"), (1, "ELICITED")]
    assert world.calls.count("vary") == 1, "the second pass is planned once"
    run = db.get(models.AuditRun, run.id)
    assert (run.status, run.total, run.done, run.stage) == ("DONE", 3, 3, 1)
    # the first rollout had nothing to learn from; the second pass did
    assert "earlier attempts" not in prompts_seen[0] and "This worked (seed 0)" in prompts_seen[-1]
    # variations are kept for the author to promote, and stay out of the next run until then
    kept = [s for s in repo.audit_seeds_list(db, "a1") if s.origin == "variation"]
    assert len(kept) == 2 and not any(s.enabled for s in kept) and {s.parent_seed_id for s in kept} == {"s0"}


def test_a_measure_run_never_deepens_or_shares_lessons(db, world, monkeypatch):
    _explore_world(world, monkeypatch)
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, seeds=("a", "b"), attacker={"models": [ATTACKER], "max_turns": 6, "best_of": 3}))
    _run_to_end(svc, run.id)
    assert len(_rollouts(db, run.id)) == 2 and "vary" not in world.calls and "pick" not in world.calls
    assert world.calls.count("attacker") == 6  # three turns each, one draft a turn


def test_best_of_n_plays_the_draft_the_picker_chose(db, world, monkeypatch):
    _explore_world(world, monkeypatch)
    world.judge_scores = {}  # CLEAN: nothing to deepen, so only the first pass is counted
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, attacker={"models": [ATTACKER], "max_turns": 6, "best_of": 3}), mode="explore")
    _run_to_end(svc, run.id)
    (r,) = _rollouts(db, run.id)
    assert r.verdict == "CLEAN" and r.detail["picks"] == 3
    assert world.calls.count("attacker") == 9 and world.calls.count("pick") == 3


def test_a_picker_that_fails_costs_only_the_choice(db, world, monkeypatch):
    judge = world.run_judge

    def fake(prompt, *, response_format, **kw):
        if response_format is explore.Pick:
            raise RuntimeError("picker is down")
        return judge(prompt, response_format=response_format, **kw)

    monkeypatch.setattr(audit_service.provider, "run_structured_agent", fake)
    world.judge_scores = {}
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, attacker={"models": [ATTACKER], "max_turns": 6, "best_of": 2}), mode="explore")
    _run_to_end(svc, run.id)
    assert _rollouts(db, run.id)[0].verdict == "CLEAN"


def test_the_first_pass_leaves_a_reserve_for_the_second(db, world):
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, seeds=("a", "b")), mode="explore", budget_usd=1.0)
    db.get(models.AuditRun, run.id).spend_usd = 0.75  # past 70%, short of the whole
    db.commit()
    _run_to_end(svc, run.id)
    assert {r.verdict for r in _rollouts(db, run.id)} == {"SKIPPED"}


def test_the_headline_run_is_the_latest_measured_one(db, world):
    from datetime import UTC, datetime, timedelta

    _audit(db)
    now = datetime.now(UTC)
    for i, mode in enumerate(("measure", "explore")):
        db.add(models.AuditRun(id=f"r{i}", audit_id="a1", project_id=PROJECT, mode=mode, created_at=now + timedelta(minutes=i)))
    db.commit()
    assert repo.audit_latest_runs(db, PROJECT)["a1"].id == "r0"
