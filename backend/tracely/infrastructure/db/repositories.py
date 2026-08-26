"""Postgres query functions for the API layer (sync sessions).

EVERY SQLAlchemy query the sync routers/services need lives here — callers open a
`SyncSessionLocal()` (owning the transaction boundary), pass the session in, and keep zero
query-building in route handlers. Functions are grouped by aggregate; they return ORM objects
(or light tuples) — HTTP serialization stays at the edge.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tracely.infrastructure.db.models import (
    Agent,
    AssistantChat,
    AgentEndpoint,
    AgentVersion,
    CaseReplay,
    ClusterMember,
    ConversationAgent,
    EvalChainProgress,
    EvaluationCase,
    EvaluationSuite,
    EvaluationSuiteCase,
    Evaluator,
    FailureCluster,
    FailureEmbedding,
    GateCase,
    GateRun,
    IngestKey,
    Invitation,
    Membership,
    MetaAnalysis,
    Monitor,
    MonitorExecution,
    MonitorStep,
    Organization,
    OrgMembership,
    Project,
    RollingSummary,
    Scenario,
    ScoreAnnotation,
    ShareRevocation,
    UsageCounter,
)

# ── projects (workspaces) ─────────────────────────────────────────────────────


def project_get(s: Session, project_id: str) -> Project | None:
    return s.get(Project, project_id)


def organization_get(s: Session, organization_id: str) -> Organization | None:
    return s.get(Organization, organization_id) if organization_id else None


def organization_by_stripe_customer(s: Session, customer_id: str) -> Organization | None:
    """The account a Stripe customer id belongs to — the webhook's primary lookup."""
    if not customer_id:
        return None
    return s.execute(
        select(Organization).where(Organization.stripe_customer_id == customer_id)
    ).scalar_one_or_none()


# ── usage counters (hosted-cloud trace quota; see services/quota_service.py) ──


def usage_increment(s: Session, project_id: str, period: str, n: int) -> None:
    """Add `n` newly-seen traces to this project's month. UPDATE-then-INSERT (with the savepoint
    IntegrityError fallback) instead of dialect `ON CONFLICT`, so the SQLite test harness runs
    the same code Postgres does. Caller commits."""
    if n <= 0:
        return
    updated = s.execute(
        update(UsageCounter)
        .where(UsageCounter.project_id == project_id, UsageCounter.period == period)
        .values(traces=UsageCounter.traces + n)
    ).rowcount
    if updated:
        return
    try:
        with s.begin_nested():
            s.add(UsageCounter(project_id=project_id, period=period, traces=n))
    except IntegrityError:
        # Two workers raced on the month's first row — the loser folds into the winner's.
        s.execute(
            update(UsageCounter)
            .where(UsageCounter.project_id == project_id, UsageCounter.period == period)
            .values(traces=UsageCounter.traces + n)
        )


def project_siblings(s: Session, project_id: str) -> list[str]:
    """Other workspaces in the same organization, oldest first. Empty for an org-less project."""
    org_id = s.execute(
        select(Project.organization_id).where(Project.id == project_id)
    ).scalar_one_or_none()
    if not org_id:
        return []
    return list(
        s.execute(
            select(Project.id)
            .where(Project.organization_id == org_id, Project.id != project_id)
            .order_by(Project.created_at, Project.id)
        ).scalars()
    )


def organization_projects(s: Session, organization_id: str) -> list[str]:
    return list(
        s.execute(
            select(Project.id)
            .where(Project.organization_id == organization_id)
            .order_by(Project.created_at, Project.id)
        ).scalars()
    )


def organization_delete(s: Session, organization_id: str) -> dict[str, int]:
    """Delete the organization row itself. Its workspaces must already be gone — the caller
    deletes each one through `project_delete` so ClickHouse events and S3 blobs go with it, which
    a database cascade could never do."""
    counts: dict[str, int] = {}
    for key, stmt in (
        ("organization_memberships",
         delete(OrgMembership).where(OrgMembership.organization_id == organization_id)),
        ("invitations",
         delete(Invitation).where(Invitation.organization_id == organization_id)),
        ("organizations", delete(Organization).where(Organization.id == organization_id)),
    ):
        n = int(s.execute(stmt).rowcount or 0)
        if n:
            counts[key] = n
    s.commit()
    return counts


def project_delete(
    s: Session, project_id: str, *, usage_heir_id: str | None
) -> dict[str, int]:
    """Delete a workspace outright: its trace-derived data, its configuration, and the row itself.

    `project_data_delete` handles everything derived; this adds what that deliberately keeps —
    ingest keys, evaluators, monitors, the legacy per-project membership/invitation rows — and
    then the project.

    `usage_heir_id` is a surviving workspace in the same org that INHERITS this one's
    `usage_counters`. Without that, deleting a workspace would zero its share of the org's
    monthly quota, making "create workspace → burn quota → delete it" an unlimited free tier.
    The caller guarantees an heir exists by refusing to delete an org's last workspace. `None` is
    only for deleting the whole organization, where there is nothing left to charge.
    """
    counts = project_data_delete(s, project_id)  # commits its own half

    if usage_heir_id:
        for row in s.execute(
            select(UsageCounter).where(UsageCounter.project_id == project_id)
        ).scalars():
            usage_increment(s, usage_heir_id, row.period, int(row.traces))

    def wipe(key: str, stmt) -> None:
        n = int(s.execute(stmt).rowcount or 0)
        if n:
            counts[key] = counts.get(key, 0) + n

    wipe("usage_counters", delete(UsageCounter).where(UsageCounter.project_id == project_id))
    # AgentEndpoint carries project_id as well as agent_id; the derived wipe only clears the ones
    # reachable through an agent, so any orphan would block the project delete on its FK.
    wipe("agent_endpoints", delete(AgentEndpoint).where(AgentEndpoint.project_id == project_id))
    wipe("evaluators", delete(Evaluator).where(Evaluator.project_id == project_id))
    wipe("monitors", delete(Monitor).where(Monitor.project_id == project_id))
    wipe("ingest_keys", delete(IngestKey).where(IngestKey.project_id == project_id))
    wipe("memberships", delete(Membership).where(Membership.project_id == project_id))
    wipe("invitations", delete(Invitation).where(Invitation.project_id == project_id))
    wipe("projects", delete(Project).where(Project.id == project_id))
    s.commit()
    return counts


def usage_traces(s: Session, project_id: str, period: str) -> int:
    row = s.get(UsageCounter, (project_id, period))
    return int(row.traces) if row else 0


def project_ingest_key(s: Session, project_id: str) -> str | None:
    """Any ingest key for this workspace — what a server-side seeder authenticates with."""
    return s.execute(
        select(IngestKey.key).where(IngestKey.project_id == project_id).limit(1)
    ).scalar_one_or_none()


def project_ui_prefs_get(s: Session, project_id: str) -> dict:
    """This workspace's UI defaults (`{}` when unset or the project is gone)."""
    proj = s.get(Project, project_id)
    return dict(proj.ui_prefs) if proj and isinstance(proj.ui_prefs, dict) else {}


def project_ui_prefs_set(s: Session, project_id: str, prefs: dict) -> dict:
    """Replace this workspace's UI defaults. Returns what was stored."""
    proj = s.get(Project, project_id)
    if proj is None:
        return {}
    proj.ui_prefs = prefs
    s.commit()
    return prefs


def project_set_openrouter_key(s: Session, project_id: str, encrypted: str | None) -> bool:
    """Set (or, with `encrypted=None`, clear) this workspace's own OpenRouter key. Returns False
    if the project doesn't exist."""
    proj = s.get(Project, project_id)
    if proj is None:
        return False
    proj.openrouter_api_key_encrypted = encrypted
    s.commit()
    return True


# ── evaluators (= evaluation columns) ─────────────────────────────────────────


def evaluators_list(s: Session, project_id: str) -> list[Evaluator]:
    return list(
        s.execute(
            select(Evaluator)
            .where(Evaluator.project_id == project_id)
            .order_by(Evaluator.created_at)
        ).scalars()
    )


def evaluator_get(s: Session, project_id: str, evaluator_id: str) -> Evaluator | None:
    e = s.get(Evaluator, evaluator_id)
    return e if e and e.project_id == project_id else None


def agent_slug(s: Session, project_id: str, agent_id: str) -> str:
    """The slug for an agent id within a project ("" if unknown) — used to match an evaluator's
    `target_agent` (a human-set slug) against a trace whose spans carry the agent id."""
    if not agent_id:
        return ""
    a = s.get(Agent, agent_id)
    return a.slug if a and a.project_id == project_id else ""


def agents_list(s: Session, project_id: str) -> list[Agent]:
    """A project's registered agents (for the meta-analysis agent selector), newest first."""
    return list(
        s.execute(
            select(Agent).where(Agent.project_id == project_id).order_by(desc(Agent.created_at))
        ).scalars()
    )


def agent_ids_with_work(s: Session, project_id: str) -> set[str]:
    """Agents that already carry authored/derived work — a scenario or a regression case.

    Paired with the trace-owner set in the agent pickers: an agent whose traces aged out or were
    deleted must not disappear from the Scenario / CI-gate selectors, or its scenarios and cases
    become unreachable from the UI while still being there.
    """
    scen = s.execute(
        select(Scenario.agent_id).where(Scenario.project_id == project_id).distinct()
    ).scalars()
    cases = s.execute(
        select(EvaluationCase.agent_id).where(EvaluationCase.project_id == project_id).distinct()
    ).scalars()
    return {a for a in [*scen, *cases] if a}


def agents_prune(s: Session, project_id: str, keep_ids: set[str]) -> list[str]:
    """Delete the project's agents that have no spans left and nothing pointing at them; returns
    the slugs removed.

    Agent rows are derived data — ingest upserts one per DECLARED agent name — so a row whose spans
    are gone (or that an older, name-inferring attribution rule invented) is junk. Anything still
    referenced (a scenario, a regression case, a configured endpoint) raises a ForeignKeyViolation
    on delete and is kept instead: one SAVEPOINT per agent turns that into "skip", so this never
    needs a hand-maintained list of the tables that point at `agents`.
    """
    pruned: list[str] = []
    for a in s.execute(select(Agent).where(Agent.project_id == project_id)).scalars().all():
        if a.id in keep_ids:
            continue
        try:
            with s.begin_nested():
                s.execute(delete(AgentVersion).where(AgentVersion.agent_id == a.id))
                s.execute(delete(Agent).where(Agent.id == a.id))
            pruned.append(a.slug)
        except IntegrityError:
            continue
    s.commit()
    return pruned


def agent_in_project(s: Session, project_id: str, agent_id: str) -> Agent | None:
    """An agent by id, scoped to the project (None if unknown / cross-tenant)."""
    a = s.get(Agent, agent_id)
    return a if a and a.project_id == project_id else None


def evaluator_score_names(s: Session, project_id: str) -> set[str]:
    return set(
        s.execute(select(Evaluator.score_name).where(Evaluator.project_id == project_id)).scalars()
    )


def advisory_score_names(s: Session, project_id: str) -> list[str]:
    """Score names of evaluators marked `config.advisory` — a FAIL on these is recorded and shown but
    does NOT flip a trace/turn/session/trend to failing (e.g. the subjective answer-quality judge).
    The per-evaluator replacement for the old hardcoded `name != 'tracely.run.quality'` magic string;
    the read layer excludes these names uniformly (see `domain.evaluation.verdict`)."""
    return [
        r.score_name
        for r in s.execute(select(Evaluator).where(Evaluator.project_id == project_id)).scalars()
        if (r.config or {}).get("advisory") is True
    ]


def evaluator_create(
    s: Session,
    project_id: str,
    *,
    name: str,
    description: str,
    kind: str,
    level: str,
    enabled: bool,
    config: dict,
    score_name: str = "",
) -> Evaluator:
    """Insert an evaluator; `score_name` (the stable scores key) is derived from the name when
    not given, and de-collided with a numeric suffix within the project."""
    taken = evaluator_score_names(s, project_id)
    resolved = (score_name or "").strip() or _slug_score_name(name)
    if resolved in taken:
        base, n = resolved, 2
        while f"{base}_{n}"[:80] in taken:
            n += 1
        resolved = f"{base}_{n}"[:80]
    e = Evaluator(
        id=str(uuid4()),
        project_id=project_id,
        name=name.strip(),
        description=description.strip(),
        kind=kind,
        score_name=resolved,
        level=level,
        enabled=enabled,
        config=config or {},
    )
    s.add(e)
    s.commit()
    s.refresh(e)
    return e


def evaluator_update(
    s: Session, project_id: str, evaluator_id: str, patch: dict
) -> Evaluator | None:
    e = evaluator_get(s, project_id, evaluator_id)
    if e is None:
        return None
    for field_name, value in patch.items():
        setattr(e, field_name, value)
    s.commit()
    s.refresh(e)
    return e


def evaluator_delete(s: Session, project_id: str, evaluator_id: str) -> bool:
    e = evaluator_get(s, project_id, evaluator_id)
    if e is None:
        return False
    s.delete(e)
    s.commit()
    return True


def chain_progress_load(s: Session, project_id: str, thread_id: str) -> dict[str, dict]:
    """Every sequential metric's progress through this thread:
    `{score_name: {turn_ids, last_payload}}` (missing metric → no progress yet)."""
    rows = s.execute(
        select(EvalChainProgress).where(
            EvalChainProgress.project_id == project_id,
            EvalChainProgress.thread_id == thread_id,
        )
    ).scalars()
    return {
        r.score_name: {
            "turn_ids": list(r.turn_ids or []),
            "last_payload": r.last_payload,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    }


def chain_progress_set(
    s: Session,
    project_id: str,
    score_name: str,
    thread_id: str,
    turn_ids: list[str],
    last_payload: dict | None,
) -> None:
    """Upsert one metric's progress (called after each newly graded turn, so a crashed pass
    resumes from the last recorded turn instead of forgetting the whole pass)."""
    row = s.get(EvalChainProgress, (project_id, score_name, thread_id))
    if row is None:
        row = EvalChainProgress(
            project_id=project_id, score_name=score_name, thread_id=thread_id
        )
        s.add(row)
    row.turn_ids = list(turn_ids)
    row.last_payload = last_payload
    s.commit()


def chain_progress_clear(
    s: Session, project_id: str, thread_id: str, score_names: list[str] | None = None
) -> None:
    """Forget progress for a thread (all metrics, or the named ones) — the companion to
    resetting the durable conversation: the next pass rebuilds from turn 1."""
    stmt = delete(EvalChainProgress).where(
        EvalChainProgress.project_id == project_id,
        EvalChainProgress.thread_id == thread_id,
    )
    if score_names:
        stmt = stmt.where(EvalChainProgress.score_name.in_(score_names))
    s.execute(stmt)
    s.commit()


def _flat_config(config: dict | None) -> dict:
    """A spec's config as ONE flat dict — the runner's contract (`base.dispatch` passes it to the
    evaluator whole). Old rows nested structural knobs under `config.params`
    ({"check": "latency", "params": {"budget_ms": …}}); fold that layer away here, the single
    place specs enter the runner, so every evaluator reads one shape and the runtime-injected
    `CFG_*` keys can never be lost to the narrowing that used to happen at dispatch."""
    config = config or {}
    params = config.get("params")
    if not isinstance(params, dict):
        return config
    return {k: v for k, v in {**config, **params}.items() if k != "params"}


def evaluator_enabled_specs(
    s: Session, project_id: str, evaluator_ids: list[str] | None = None
) -> list[dict]:
    """The runner's view of a project's enabled evaluators (optionally narrowed by id),
    creation-ordered so sequential chaining is deterministic.

    When narrowed by `evaluator_ids`, the selection is expanded to also include any enabled
    evaluators it `config.depends_on` (transitively, by `score_name`): a dependent can't be
    graded without its prerequisites' results, so they're pulled into the run and topo-sorted
    to execute first (see `evaluation_service._topo_sort`). Disabled dependencies aren't run —
    the dependent simply grades without that context."""
    all_specs = [
        {
            "id": r.id,
            "kind": r.kind,
            "config": _flat_config(r.config),
            "score_name": r.score_name,
            "level": r.level,
            # targeting + sampling — the runner applies these on the auto (on-ingest) run
            "target_agent": r.target_agent or "",
            "target_env": r.target_env or "",
            "sampling": r.sampling if r.sampling is not None else 1.0,
        }
        for r in s.execute(
            select(Evaluator)
            .where(Evaluator.project_id == project_id, Evaluator.enabled.is_(True))
            .order_by(Evaluator.created_at)
        ).scalars()
    ]
    if not evaluator_ids:
        return all_specs
    by_name = {spec["score_name"]: spec for spec in all_specs}
    wanted = set(evaluator_ids)
    selected_ids = {spec["id"] for spec in all_specs if spec["id"] in wanted}
    frontier = [spec for spec in all_specs if spec["id"] in selected_ids]
    while frontier:  # transitively pull in dependencies so prerequisites run too
        spec = frontier.pop()
        for dep_name in spec["config"].get("depends_on") or []:
            dep = by_name.get(dep_name)
            if dep and dep["id"] not in selected_ids:
                selected_ids.add(dep["id"])
                frontier.append(dep)
    return [spec for spec in all_specs if spec["id"] in selected_ids]  # creation-ordered


def _slug_score_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "metric").lower()).strip("_")
    return f"custom.{(base or 'metric')[:60]}"


# ── regression cases ──────────────────────────────────────────────────────────


def cases_list(
    s: Session, project_id: str, limit: int | None = None, offset: int = 0
) -> list[EvaluationCase]:
    q = (
        select(EvaluationCase)
        .where(EvaluationCase.project_id == project_id)
        .order_by(desc(EvaluationCase.created_at), EvaluationCase.id)
    )
    if limit is not None:
        q = q.limit(limit).offset(max(offset, 0))
    return list(s.execute(q).scalars())


def cases_count(s: Session, project_id: str) -> int:
    return int(
        s.execute(
            select(func.count())
            .select_from(EvaluationCase)
            .where(EvaluationCase.project_id == project_id)
        ).scalar_one()
    )


def cases_count_by_agent(s: Session, project_id: str) -> dict[str, int]:
    """`{agent_id: promoted cases}` — what the gate launcher ranks agents by and labels its
    picker with. A GROUP BY rather than a tally over the case list, so it stays correct once that
    list is paginated. PROMOTED only: those are the cases a gate actually replays."""
    return {
        aid: int(n)
        for aid, n in s.execute(
            select(EvaluationCase.agent_id, func.count())
            .where(
                EvaluationCase.project_id == project_id,
                EvaluationCase.status == "PROMOTED",
            )
            .group_by(EvaluationCase.agent_id)
        ).all()
    }


def case_get(s: Session, project_id: str, case_id: str) -> EvaluationCase | None:
    c = s.get(EvaluationCase, case_id)
    return c if c and c.project_id == project_id else None


def case_for_trace(s: Session, project_id: str, trace_id: str) -> EvaluationCase | None:
    """The case promoted FROM this trace, if any — what lets the trace page offer
    "remove from regression" instead of a second promote."""
    return (
        s.execute(
            select(EvaluationCase)
            .where(
                EvaluationCase.project_id == project_id,
                EvaluationCase.source_trace_id == trace_id,
            )
            .order_by(desc(EvaluationCase.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )


def case_last_replay(s: Session, case_id: str) -> CaseReplay | None:
    return (
        s.execute(
            select(CaseReplay)
            .where(CaseReplay.case_id == case_id)
            .order_by(desc(CaseReplay.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )


def case_replays(s: Session, case_id: str, limit: int = 50) -> list[CaseReplay]:
    """The case's replay history, newest first. Capped: a case gated on every PR accumulates a
    replay per run for ever, and the detail page only ever shows the recent ones."""
    return list(
        s.execute(
            select(CaseReplay)
            .where(CaseReplay.case_id == case_id)
            .order_by(desc(CaseReplay.created_at))
            .limit(limit)
        ).scalars()
    )


def case_delete(s: Session, project_id: str, case_id: str) -> bool:
    """Delete one regression case plus everything that points at it (replays, suite membership,
    per-gate verdicts). False if the case is unknown or belongs to another project.

    Past gate *runs* survive with their counters intact — only the deleted case's verdict row goes,
    so a historical gate can end up listing fewer cases than its `total`. That's honest: the case
    no longer exists to explain.

    ponytail: leaves the case's S3 fixture bundle. Blobs are cheap and orphaned ones are harmless;
    add a sweep if storage cost ever shows up.
    """
    c = case_get(s, project_id, case_id)
    if c is None:
        return False
    s.execute(delete(GateCase).where(GateCase.evaluation_case_id == case_id))
    s.execute(delete(EvaluationSuiteCase).where(EvaluationSuiteCase.case_id == case_id))
    s.execute(delete(CaseReplay).where(CaseReplay.case_id == case_id))
    s.delete(c)
    s.commit()
    return True


# ── failure clusters ──────────────────────────────────────────────────────────


def clusters_list_with_agent(
    s: Session, project_id: str, min_size: int = 1,
    limit: int | None = None, offset: int = 0,
) -> list[tuple[FailureCluster, str]]:
    """`min_size` hides clusters with fewer members — a failure seen once is noise, not an issue."""
    q = (
        select(FailureCluster, Agent.slug)
        .join(Agent, FailureCluster.agent_id == Agent.id)
        .where(
            FailureCluster.project_id == project_id,
            FailureCluster.count >= min_size,
        )
        # `id` breaks ties so LIMIT/OFFSET paging can't repeat or skip a cluster between pages.
        .order_by(desc(FailureCluster.count), desc(FailureCluster.last_seen_at), FailureCluster.id)
    )
    if limit is not None:
        q = q.limit(limit).offset(max(offset, 0))
    return [(cl, slug) for cl, slug in s.execute(q).all()]


def clusters_count(
    s: Session, project_id: str, min_size: int = 1, status: str | None = None
) -> int:
    q = (
        select(func.count())
        .select_from(FailureCluster)
        .join(Agent, FailureCluster.agent_id == Agent.id)
        .where(
            FailureCluster.project_id == project_id,
            FailureCluster.count >= min_size,
        )
    )
    if status:
        q = q.where(FailureCluster.status == status)
    return int(s.execute(q).scalar_one())


def cluster_get(s: Session, project_id: str, cluster_id: str) -> FailureCluster | None:
    cl = s.get(FailureCluster, cluster_id)
    return cl if cl and cl.project_id == project_id else None


def cluster_members(
    s: Session, cluster_id: str, limit: int | None = None
) -> list[ClusterMember]:
    """The cluster's traces, medoid first. `limit` caps the render on the detail page — a cluster
    that fired 5,000 times must not ship 5,000 rows to the browser; promotion only needs the
    medoid, and the count is shown from `cluster.count` regardless."""
    q = (
        select(ClusterMember)
        .where(ClusterMember.cluster_id == cluster_id)
        .order_by(desc(ClusterMember.is_medoid), ClusterMember.added_at)
    )
    if limit is not None:
        q = q.limit(limit)
    return list(s.execute(q).scalars())


def clusters_delete(s: Session, project_id: str, cluster_ids: list[str]) -> int:
    """Delete clusters (and their members) in this project. Returns how many were removed.

    Clusters are derived data: as long as the failing traces are still there, the next Analyze
    re-forms them. Deleting is for pruning noise and orphans (issues whose traces were deleted)."""
    if not cluster_ids:
        return 0
    owned = list(
        s.execute(
            select(FailureCluster.id).where(
                FailureCluster.project_id == project_id, FailureCluster.id.in_(cluster_ids)
            )
        ).scalars()
    )
    if not owned:
        return 0
    s.execute(delete(ClusterMember).where(ClusterMember.cluster_id.in_(owned)))
    s.execute(delete(FailureCluster).where(FailureCluster.id.in_(owned)))
    s.commit()
    return len(owned)


def cluster_medoid(s: Session, cluster_id: str) -> ClusterMember | None:
    members = cluster_members(s, cluster_id)
    return members[0] if members else None


# ── gates ─────────────────────────────────────────────────────────────────────


def gates_list_with_agent(
    s: Session, project_id: str, limit: int | None = None, offset: int = 0
) -> list[tuple[GateRun, str]]:
    q = (
        select(GateRun, Agent.slug)
        .join(Agent, GateRun.agent_id == Agent.id)
        .where(GateRun.project_id == project_id)
        # Gates are the fastest-growing table in the product — one row per agent per PR run. Ties
        # on created_at are broken by id so paging is stable.
        .order_by(desc(GateRun.created_at), GateRun.id)
    )
    if limit is not None:
        q = q.limit(limit).offset(max(offset, 0))
    return [(g, slug) for g, slug in s.execute(q).all()]


def gates_count(s: Session, project_id: str) -> int:
    return int(
        s.execute(
            select(func.count())
            .select_from(GateRun)
            .join(Agent, GateRun.agent_id == Agent.id)
            .where(GateRun.project_id == project_id)
        ).scalar_one()
    )


def gate_cases_with_titles(s: Session, gate_id: str) -> list[tuple[GateCase, str]]:
    """Every graded unit in a gate run, with a display title.

    OUTER joins on purpose: a gate case is either a replayed regression case or an emulated
    conversation, so exactly one side is ever populated. An inner join on `evaluation_cases`
    (what this used to be) silently dropped every scenario row — the conversations would run,
    block the merge, and then be invisible in the gate detail and the PR comment.
    """
    return [
        (gc, case_title or scenario_title or "(untitled)")
        for gc, case_title, scenario_title in s.execute(
            select(GateCase, EvaluationCase.title, Scenario.title)
            .outerjoin(EvaluationCase, GateCase.evaluation_case_id == EvaluationCase.id)
            .outerjoin(Scenario, GateCase.scenario_id == Scenario.id)
            .where(GateCase.gate_run_id == gate_id)
        ).all()
    ]


# ── scenarios (emulated conversations) ────────────────────────────────────────


def scenarios_list(s: Session, project_id: str, agent_id: str | None = None) -> list[Scenario]:
    q = select(Scenario).where(Scenario.project_id == project_id)
    if agent_id:
        q = q.where(Scenario.agent_id == agent_id)
    return list(s.execute(q.order_by(Scenario.created_at.desc())).scalars())


def scenario_delete(s: Session, project_id: str, scenario_id: str) -> bool:
    sc = s.get(Scenario, scenario_id)
    if sc is None or sc.project_id != project_id:
        return False
    # Detach from any gate case that recorded it — a past gate result stays readable after the
    # scenario itself is deleted (the verdict and trace ids live on the GateCase row).
    s.execute(
        update(GateCase).where(GateCase.scenario_id == scenario_id).values(scenario_id=None)
    )
    s.delete(sc)
    s.commit()
    return True


# ── search (⌘K registry side) ─────────────────────────────────────────────────


def search_registry(s: Session, project_id: str, q: str) -> list[dict]:
    """Issues, cases, and gates matching the query — pre-shaped for the ⌘K palette."""
    like = f"%{q}%"
    rows: list[dict] = []
    for cl in s.execute(
        select(FailureCluster)
        .where(FailureCluster.project_id == project_id, FailureCluster.label.ilike(like))
        .limit(6)
    ).scalars():
        rows.append(
            {
                "type": "issue",
                "label": cl.label,
                "sub": cl.taxonomy or "",
                "href": f"/clusters/{cl.id}",
            }
        )
    for c in s.execute(
        select(EvaluationCase)
        .where(EvaluationCase.project_id == project_id, EvaluationCase.title.ilike(like))
        .limit(6)
    ).scalars():
        rows.append(
            {
                "type": "case",
                "label": c.title,
                "sub": c.status,
                "href": f"/cases/{c.id}",
            }
        )
    for g in s.execute(
        select(GateRun)
        .where(GateRun.project_id == project_id, GateRun.git_ref.ilike(like))
        .order_by(desc(GateRun.created_at))
        .limit(4)
    ).scalars():
        rows.append(
            {
                "type": "gate",
                "label": g.git_ref or g.id[:8],
                "sub": g.status,
                "href": f"/gates/{g.id}",
            }
        )
    return rows


# ── dashboard / trends rollups ────────────────────────────────────────────────


def registry_counts(s: Session, project_id: str) -> dict:
    agents = (
        s.execute(
            select(func.count()).select_from(Agent).where(Agent.project_id == project_id)
        ).scalar()
        or 0
    )
    cases = (
        s.execute(
            select(func.count())
            .select_from(EvaluationCase)
            .where(EvaluationCase.project_id == project_id)
        ).scalar()
        or 0
    )
    open_clusters = (
        s.execute(
            select(func.count())
            .select_from(FailureCluster)
            .where(FailureCluster.project_id == project_id, FailureCluster.status == "OPEN")
        ).scalar()
        or 0
    )
    return {"agents": int(agents), "cases": int(cases), "open_clusters": int(open_clusters)}


def gate_cluster_trends(s: Session, project_id: str) -> dict:
    """The Postgres side of /api/trends: gate pass-rate + per-day outcomes, cluster counts,
    case count, and the MTTR proxy (cluster first-seen → promoted regression case)."""
    from collections import defaultdict

    gates = list(s.execute(select(GateRun).where(GateRun.project_id == project_id)).scalars())
    gate_total = len(gates)
    gate_passed = sum(1 for g in gates if g.status == "PASS")
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [passed, failed]
    for g in gates:
        if g.created_at and g.status in ("PASS", "FAIL"):
            by_day[g.created_at.date().isoformat()][0 if g.status == "PASS" else 1] += 1
    gates_daily = [{"date": k, "passed": v[0], "failed": v[1]} for k, v in sorted(by_day.items())]

    clusters = list(
        s.execute(select(FailureCluster).where(FailureCluster.project_id == project_id)).scalars()
    )
    open_c = sum(1 for cl in clusters if cl.status == "OPEN")
    resolved_c = sum(1 for cl in clusters if cl.status == "PROMOTED")
    all_cases = list(
        s.execute(select(EvaluationCase).where(EvaluationCase.project_id == project_id)).scalars()
    )

    # MTTR proxy: hours from a cluster's first-seen to the regression case it was promoted into
    case_created = {c.id: c.created_at for c in all_cases}
    spans: list[float] = []
    for cl in clusters:
        if cl.status == "PROMOTED" and cl.candidate_case_id and cl.first_seen_at:
            created = case_created.get(cl.candidate_case_id)
            if created:
                h = (created - cl.first_seen_at).total_seconds() / 3600.0
                if h >= 0:
                    spans.append(h)
    mttr_hours = round(sum(spans) / len(spans), 1) if spans else None

    return {
        "gates_daily": gates_daily,
        "gate_runs": gate_total,
        "gate_pass_rate": round(gate_passed / gate_total, 3) if gate_total else 0.0,
        "cases": len(all_cases),
        "open_clusters": open_c,
        "resolved_clusters": resolved_c,
        "mttr_hours": mttr_hours,
    }


# ── meta-analyses ─────────────────────────────────────────────────────────────


def meta_analysis_create(
    s: Session, project_id: str, *, agent_id: str, result: dict, meta: dict
) -> MetaAnalysis:
    """Persist a meta-analysis result. A fresh row per run (history is kept); the UI reads the
    latest via `meta_analysis_latest_for_agent`."""
    ma = MetaAnalysis(
        id=str(uuid4()),
        project_id=project_id,
        agent_id=agent_id or "",
        analysis_type="agent",
        result=result or {},
        meta=meta or {},
    )
    s.add(ma)
    s.commit()
    s.refresh(ma)
    return ma


def meta_analysis_latest_for_agent(
    s: Session, project_id: str, agent_id: str
) -> MetaAnalysis | None:
    """The most recent analysis for this (project, agent) — what the panel shows on open."""
    return (
        s.execute(
            select(MetaAnalysis)
            .where(
                MetaAnalysis.project_id == project_id,
                MetaAnalysis.agent_id == (agent_id or ""),
            )
            .order_by(desc(MetaAnalysis.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )


def meta_analysis_get(s: Session, project_id: str, analysis_id: str) -> MetaAnalysis | None:
    ma = s.get(MetaAnalysis, analysis_id)
    return ma if ma and ma.project_id == project_id else None


def meta_analysis_delete(s: Session, project_id: str, analysis_id: str) -> bool:
    ma = meta_analysis_get(s, project_id, analysis_id)
    if ma is None:
        return False
    s.delete(ma)
    s.commit()
    return True


# ── rolling summaries ─────────────────────────────────────────────────────────


def rolling_summary_get_by_span(s: Session, project_id: str, span_id: str) -> RollingSummary | None:
    return (
        s.execute(
            select(RollingSummary).where(
                RollingSummary.project_id == project_id, RollingSummary.span_id == span_id
            )
        )
        .scalars()
        .first()
    )


def rolling_summary_latest_for_thread(
    s: Session, project_id: str, thread_id: str
) -> RollingSummary | None:
    """The highest-step_order row = the whole-conversation summary."""
    return (
        s.execute(
            select(RollingSummary)
            .where(RollingSummary.project_id == project_id, RollingSummary.thread_id == thread_id)
            .order_by(desc(RollingSummary.step_order), desc(RollingSummary.created_at))
            .limit(1)
        )
        .scalars()
        .first()
    )


def rolling_summary_latest_before(
    s: Session, project_id: str, thread_id: str, step_order: int
) -> RollingSummary | None:
    """The accumulated summary strictly before `step_order` — seeds continued accumulation."""
    return (
        s.execute(
            select(RollingSummary)
            .where(
                RollingSummary.project_id == project_id,
                RollingSummary.thread_id == thread_id,
                RollingSummary.step_order < step_order,
            )
            .order_by(desc(RollingSummary.step_order))
            .limit(1)
        )
        .scalars()
        .first()
    )


def rolling_summary_list_for_thread(
    s: Session, project_id: str, thread_id: str
) -> list[RollingSummary]:
    return list(
        s.execute(
            select(RollingSummary)
            .where(RollingSummary.project_id == project_id, RollingSummary.thread_id == thread_id)
            .order_by(RollingSummary.step_order)
        ).scalars()
    )


def rolling_summary_create(
    s: Session,
    project_id: str,
    *,
    thread_id: str,
    trace_id: str,
    span_id: str,
    step_order: int,
    summary: list,
    token_count: int,
    meta: dict,
) -> RollingSummary:
    """Insert one step's accumulated summary. Race-safe: a concurrent writer that already inserted
    this span (unique project+span) makes us roll back and return the existing row."""
    rs = RollingSummary(
        id=str(uuid4()),
        project_id=project_id,
        thread_id=thread_id,
        trace_id=trace_id or "",
        span_id=span_id,
        step_order=step_order,
        summary=summary or [],
        token_count=token_count,
        meta=meta or {},
    )
    s.add(rs)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        existing = rolling_summary_get_by_span(s, project_id, span_id)
        if existing is not None:
            return existing
        raise
    s.refresh(rs)
    return rs


def rolling_summary_delete_for_thread(s: Session, project_id: str, thread_id: str) -> int:
    """Drop a thread's summaries (force-regenerate). Returns rows removed."""
    rows = rolling_summary_list_for_thread(s, project_id, thread_id)
    for r in rows:
        s.delete(r)
    s.commit()
    return len(rows)


# ── conversation agents (user-declared catalog) ───────────────────────────────


def conversation_agents_get(
    s: Session, project_id: str, thread_id: str
) -> ConversationAgent | None:
    return (
        s.execute(
            select(ConversationAgent).where(
                ConversationAgent.project_id == project_id,
                ConversationAgent.thread_id == thread_id,
            )
        )
        .scalars()
        .first()
    )


def conversation_agents_upsert(
    s: Session, project_id: str, *, thread_id: str, agents: list, meta: dict | None = None
) -> ConversationAgent:
    """Insert or replace a conversation's declared agent catalog (latest wins per thread)."""
    row = conversation_agents_get(s, project_id, thread_id)
    if row is None:
        row = ConversationAgent(
            id=str(uuid4()),
            project_id=project_id,
            thread_id=thread_id,
            agents=agents or [],
            meta=meta or {},
        )
        s.add(row)
    else:
        row.agents = agents or []
        if meta is not None:
            row.meta = meta
    s.commit()
    s.refresh(row)
    return row


# ── score annotations (judge-vs-human calibration) ──────────────────────────────
def _annotation_key(
    q, project_id, score_name, evaluation_level, trace_id, session_id, observation_id, labeled_by
):
    return q.where(
        ScoreAnnotation.project_id == project_id,
        ScoreAnnotation.score_name == score_name,
        ScoreAnnotation.evaluation_level == evaluation_level,
        ScoreAnnotation.trace_id == trace_id,
        ScoreAnnotation.session_id == session_id,
        ScoreAnnotation.observation_id == observation_id,
        ScoreAnnotation.labeled_by == labeled_by,
    )


def score_annotation_upsert(
    s: Session,
    project_id: str,
    *,
    score_name: str,
    human_verdict: str,
    evaluation_level: str = "",
    trace_id: str = "",
    session_id: str = "",
    observation_id: str = "",
    judge_verdict: str = "",
    note: str | None = None,
    labeled_by: str = "",
) -> ScoreAnnotation:
    """Insert or replace one reviewer's label on a judge score (keyed by the score's natural identity
    + labeler). `judge_verdict` is snapshotted so agreement reflects what the human reviewed."""
    row = _annotation_key(
        select(ScoreAnnotation),
        project_id,
        score_name,
        evaluation_level,
        trace_id,
        session_id,
        observation_id,
        labeled_by,
    )
    row = s.execute(row).scalar_one_or_none()
    if row is None:
        row = ScoreAnnotation(
            id=str(uuid4()),
            project_id=project_id,
            score_name=score_name,
            evaluation_level=evaluation_level,
            trace_id=trace_id,
            session_id=session_id,
            observation_id=observation_id,
            judge_verdict=judge_verdict,
            human_verdict=human_verdict,
            note=note,
            labeled_by=labeled_by,
        )
        s.add(row)
    else:
        row.judge_verdict = judge_verdict
        row.human_verdict = human_verdict
        row.note = note
    s.commit()
    s.refresh(row)
    return row


def score_annotation_delete(
    s: Session,
    project_id: str,
    *,
    score_name: str,
    evaluation_level: str = "",
    trace_id: str = "",
    session_id: str = "",
    observation_id: str = "",
    labeled_by: str = "",
) -> bool:
    """Remove a reviewer's label (clearing it). Returns whether a row was deleted."""
    row = _annotation_key(
        select(ScoreAnnotation),
        project_id,
        score_name,
        evaluation_level,
        trace_id,
        session_id,
        observation_id,
        labeled_by,
    )
    row = s.execute(row).scalar_one_or_none()
    if row is None:
        return False
    s.delete(row)
    s.commit()
    return True


def score_annotations_for_trace(
    s: Session,
    project_id: str,
    *,
    trace_id: str = "",
    session_id: str = "",
    labeled_by: str | None = None,
) -> list[ScoreAnnotation]:
    """Existing labels on a trace and/or its thread (optionally just one reviewer's) — used to render
    the current annotation state in the UI."""
    q = select(ScoreAnnotation).where(ScoreAnnotation.project_id == project_id)
    keys = []
    if trace_id:
        keys.append(ScoreAnnotation.trace_id == trace_id)
    if session_id:
        keys.append(ScoreAnnotation.session_id == session_id)
    if keys:
        q = q.where(or_(*keys))
    if labeled_by is not None:
        q = q.where(ScoreAnnotation.labeled_by == labeled_by)
    return list(s.execute(q).scalars().all())


def score_annotations_for_project(
    s: Session, project_id: str, score_name: str | None = None
) -> list[ScoreAnnotation]:
    """All labels in a project (optionally one evaluator) — the input to the agreement computation."""
    q = select(ScoreAnnotation).where(ScoreAnnotation.project_id == project_id)
    if score_name:
        q = q.where(ScoreAnnotation.score_name == score_name)
    return list(s.execute(q.order_by(desc(ScoreAnnotation.updated_at))).scalars().all())


# ── monitors ──────────────────────────────────────────────────────────────────


def monitors_list(s: Session, project_id: str) -> list[Monitor]:
    """A project's monitors, oldest first (CRUD UI ordering matches creation)."""
    return list(
        s.execute(
            select(Monitor).where(Monitor.project_id == project_id).order_by(Monitor.created_at)
        ).scalars()
    )


def monitor_get(s: Session, project_id: str, monitor_id: str) -> Monitor | None:
    m = s.get(Monitor, monitor_id)
    return m if m and m.project_id == project_id else None


def monitor_create(
    s: Session,
    project_id: str,
    *,
    name: str,
    description: str,
    target_agent: str,
    condition: dict,
    channels: list,
    enabled: bool,
    min_interval_seconds: int,
) -> Monitor:
    m = Monitor(
        id=str(uuid4()),
        project_id=project_id,
        name=name.strip(),
        description=description.strip(),
        target_agent=(target_agent or "").strip(),
        condition=condition or {},
        channels=channels or [],
        enabled=enabled,
        min_interval_seconds=max(int(min_interval_seconds or 0), 0),
    )
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


def monitor_update(s: Session, project_id: str, monitor_id: str, patch: dict) -> Monitor | None:
    m = monitor_get(s, project_id, monitor_id)
    if m is None:
        return None
    for field_name, value in patch.items():
        setattr(m, field_name, value)
    s.commit()
    s.refresh(m)
    return m


def monitor_delete(s: Session, project_id: str, monitor_id: str) -> bool:
    m = monitor_get(s, project_id, monitor_id)
    if m is None:
        return False
    s.delete(m)
    s.commit()
    return True


def monitor_steps_replace(s: Session, monitor: Monitor, steps: list[dict]) -> None:
    """Replace a monitor's steps wholesale with the canvas's list.

    Delete-all-then-reinsert, and the ids come BACK (a step id is the canvas node id, so an edit
    keeps it). That is why the delete has to be flushed before the inserts: same primary keys in
    one flush is an identity-map conflict, and SQLAlchemy raises rather than ordering it for you.
    """
    s.execute(delete(MonitorStep).where(MonitorStep.monitor_id == monitor.id))
    s.flush()
    for i, step in enumerate(steps):
        s.add(
            MonitorStep(
                id=str(step.get("id") or uuid4()),
                monitor_id=monitor.id,
                order_index=int(step.get("order_index", i)),
                name=str(step.get("name") or f"Step {i + 1}"),
                step_type=str(step.get("step_type") or ""),
                config=step.get("config") or {},
            )
        )
    s.flush()
    s.expire(monitor, ["steps"])


def monitor_executions(
    s: Session, project_id: str, monitor_id: str, limit: int = 20
) -> list[MonitorExecution]:
    """A monitor's runs, newest first — the "what did this alert actually do" log."""
    return list(
        s.execute(
            select(MonitorExecution)
            .where(
                MonitorExecution.project_id == project_id,
                MonitorExecution.monitor_id == monitor_id,
            )
            .order_by(desc(MonitorExecution.started_at))
            .limit(limit)
        ).scalars()
    )


def enabled_monitors_across_projects(s: Session) -> list[Monitor]:
    """Every enabled monitor across every project — the worker's fan-out input. Cheap (small
    table, indexed `(project_id, enabled)`)."""
    return list(s.execute(select(Monitor).where(Monitor.enabled.is_(True))).scalars())


# ── project reset ─────────────────────────────────────────────────────────────


def project_data_delete(s: Session, project_id: str) -> dict[str, int]:
    """Delete everything DERIVED FROM TRACES in this project; keep everything CONFIGURED BY HAND.

    Gone: agents (+versions), regression cases (+replays/suites), gate runs, failure clusters
    (+members/embeddings), meta-analyses, rolling summaries, conversation agents, score
    annotations. Agents count as derived — they auto-register on ingest and come straight back
    with the next trace.

    Kept: the project, ingest keys, users/memberships, evaluators and monitors. Those are your
    setup, not your data — wiping them would mean reconfiguring the workspace to use it again.
    Evaluators/monitors target agents by slug (no FK), so they survive an agent wipe intact.

    ALSO kept, deliberately and forever: `usage_counters`. It looks trace-derived, but it is the
    billing record — wiping it here would make Data → wipe a self-serve monthly quota reset.

    Returns per-table row counts. Caller is responsible for the two halves that do not live in
    this session: ClickHouse (`infrastructure.clickhouse.deletes.delete_project_events`) and the
    judge's conversations (`infrastructure.llm.checkpointer.delete_project_chats`) — the latter
    pairs with the `eval_chain_progress` rows cleared here, and holds the same messages verbatim.
    """
    case_ids = list(
        s.execute(
            select(EvaluationCase.id).where(EvaluationCase.project_id == project_id)
        ).scalars()
    )
    gate_ids = list(s.execute(select(GateRun.id).where(GateRun.project_id == project_id)).scalars())
    cluster_ids = list(
        s.execute(
            select(FailureCluster.id).where(FailureCluster.project_id == project_id)
        ).scalars()
    )
    agent_ids = list(s.execute(select(Agent.id).where(Agent.project_id == project_id)).scalars())
    suite_ids = list(
        s.execute(
            select(EvaluationSuite.id).where(EvaluationSuite.project_id == project_id)
        ).scalars()
    )

    counts: dict[str, int] = {}

    def wipe(key: str, stmt) -> None:
        counts[key] = int(s.execute(stmt).rowcount or 0)

    # children first — plain FKs, no ON DELETE CASCADE in the schema
    if gate_ids:
        wipe("gate_cases", delete(GateCase).where(GateCase.gate_run_id.in_(gate_ids)))
    if case_ids:
        wipe("case_replays", delete(CaseReplay).where(CaseReplay.case_id.in_(case_ids)))
    if suite_ids:
        wipe(
            "evaluation_suite_cases",
            delete(EvaluationSuiteCase).where(EvaluationSuiteCase.suite_id.in_(suite_ids)),
        )
    if cluster_ids:
        wipe(
            "cluster_members",
            delete(ClusterMember).where(ClusterMember.cluster_id.in_(cluster_ids)),
        )

    wipe("gate_runs", delete(GateRun).where(GateRun.project_id == project_id))
    wipe("evaluation_cases", delete(EvaluationCase).where(EvaluationCase.project_id == project_id))
    wipe(
        "evaluation_suites", delete(EvaluationSuite).where(EvaluationSuite.project_id == project_id)
    )
    wipe("failure_clusters", delete(FailureCluster).where(FailureCluster.project_id == project_id))
    wipe(
        "failure_embeddings",
        delete(FailureEmbedding).where(FailureEmbedding.project_id == project_id),
    )
    wipe("meta_analyses", delete(MetaAnalysis).where(MetaAnalysis.project_id == project_id))
    wipe("rolling_summaries", delete(RollingSummary).where(RollingSummary.project_id == project_id))
    wipe("eval_chain_progress",
         delete(EvalChainProgress).where(EvalChainProgress.project_id == project_id))
    wipe(
        "conversation_agents",
        delete(ConversationAgent).where(ConversationAgent.project_id == project_id),
    )
    wipe(
        "score_annotations", delete(ScoreAnnotation).where(ScoreAnnotation.project_id == project_id)
    )
    # The assistant's conversations quote this workspace's traces and can carry uploaded files;
    # "delete all project data" that leaves them behind is not a delete.
    wipe("assistant_chats", delete(AssistantChat).where(AssistantChat.project_id == project_id))
    # Everything that FKs to `agents` must go before it. Miss one and the whole wipe raises a
    # ForeignKeyViolation and rolls back — the button reports "internal server error" and NOTHING
    # is deleted, which reads as "the delete silently did nothing".
    wipe("scenarios", delete(Scenario).where(Scenario.project_id == project_id))
    if agent_ids:
        wipe(
            "agent_endpoints", delete(AgentEndpoint).where(AgentEndpoint.agent_id.in_(agent_ids))
        )
        wipe("agent_versions", delete(AgentVersion).where(AgentVersion.agent_id.in_(agent_ids)))
    wipe("agents", delete(Agent).where(Agent.project_id == project_id))

    s.commit()
    return {k: v for k, v in counts.items() if v}


# --------------------------------------------------------------------------------------------
# In-app assistant conversations
# --------------------------------------------------------------------------------------------
# Scoped to (project, owner). `user_id` is None for machine callers and dev mode, and NULL never
# equals NULL in SQL — so the None case has to be an IS NULL, not an `== None` that silently
# matches no row and hands every dev-mode caller a permanently empty history.


def _chat_owned(project_id: str, user_id: str | None):
    owner = AssistantChat.user_id.is_(None) if user_id is None else AssistantChat.user_id == user_id
    return (AssistantChat.project_id == project_id) & owner


def assistant_chat_list(
    s: Session, project_id: str, user_id: str | None, limit: int = 50
) -> list[AssistantChat]:
    """Newest first — the order the history panel shows them in."""
    return list(
        s.execute(
            select(AssistantChat)
            .where(_chat_owned(project_id, user_id))
            .order_by(AssistantChat.updated_at.desc())
            .limit(limit)
        ).scalars()
    )


def assistant_chat_get(
    s: Session, project_id: str, user_id: str | None, chat_id: str
) -> AssistantChat | None:
    return s.execute(
        select(AssistantChat).where(_chat_owned(project_id, user_id) & (AssistantChat.id == chat_id))
    ).scalar_one_or_none()


def assistant_chat_save(
    s: Session,
    project_id: str,
    user_id: str | None,
    *,
    chat_id: str | None,
    messages: list,
    title: str,
) -> AssistantChat:
    """Create or overwrite a conversation. A `chat_id` that isn't this owner's starts a new chat
    rather than raising — the alternative is one guessed id reading or overwriting someone
    else's conversation."""
    row = assistant_chat_get(s, project_id, user_id, chat_id) if chat_id else None
    if row is None:
        row = AssistantChat(
            id=str(uuid4()), project_id=project_id, user_id=user_id, title=title[:120]
        )
        s.add(row)
    row.messages = messages
    if title and not row.title:
        row.title = title[:120]
    s.commit()
    s.refresh(row)
    return row


def assistant_chat_delete(
    s: Session, project_id: str, user_id: str | None, chat_id: str
) -> bool:
    row = assistant_chat_get(s, project_id, user_id, chat_id)
    if row is None:
        return False
    s.delete(row)
    s.commit()
    return True



# ── public share links ────────────────────────────────────────────────────────


def share_revoked_at(s: Session, project_id: str, kind: str, subject_id: str) -> datetime | None:
    """When sharing was stopped for this subject, if it ever was."""
    row = s.get(ShareRevocation, (project_id, kind, subject_id))
    return row.revoked_at if row else None


def share_revoke(s: Session, project_id: str, kind: str, subject_id: str) -> None:
    """Kill every link minted for this subject so far. Idempotent; re-revoking moves the line
    forward, which also kills anything minted since the last call."""
    row = s.get(ShareRevocation, (project_id, kind, subject_id))
    if row is None:
        row = ShareRevocation(project_id=project_id, kind=kind, subject_id=subject_id)
        s.add(row)
    row.revoked_at = datetime.now(UTC)
    s.commit()
