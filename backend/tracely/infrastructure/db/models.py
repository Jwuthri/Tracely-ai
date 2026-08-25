"""Postgres registry models (SQLAlchemy 2.0). Canonical entities per design 00/09.

Enums are stored as String for migration simplicity; values are the canonical sets.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tracely.config import settings
from tracely.infrastructure.db.base import Base


class AgentKind(str, enum.Enum):
    SINGLE = "SINGLE"
    MULTI_AGENT = "MULTI_AGENT"
    WORKFLOW = "WORKFLOW"


class AgentRole(str, enum.Enum):
    SUPERVISOR = "SUPERVISOR"
    WORKER = "WORKER"
    PLANNER = "PLANNER"
    EXECUTOR = "EXECUTOR"
    GENERIC = "GENERIC"


class Organization(Base):
    """The account a person or company signs up as — the tier ABOVE workspaces (migration 0023).

    People are members of an organization, never of a single workspace: access to a project is
    derived from membership in the project's organization (`auth/principal.select_membership`).
    That makes cross-tenant access structurally impossible rather than policed — there is no
    "invite someone to one workspace" path to get wrong.

    `kind` decides the shape of the account:
      - `personal` — one human, exactly 1 workspace and 1 seat. Cannot be joined, ever.
      - `company`  — a team: several workspaces and seats, bounded by the plan.
    Billing lives here too (plan + Stripe subscription): a company buys one subscription, not one
    per workspace, and the monthly trace quota is the sum over the org's workspaces.
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="personal", server_default="personal")
    # `free | pro | unlimited` — `unlimited` is for operator orgs (set via SQL) and is never
    # written by webhooks.
    plan: Mapped[str] = mapped_column(String(16), default="free", server_default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # `created` of the newest Stripe event applied to this org (unix seconds). Stripe does NOT
    # guarantee delivery order and retries for days, so an older event arriving after a newer one
    # would re-apply a stale plan — a cancelled subscription quietly going back to Pro. NULL on
    # every row that predates this, which just means the next event is the first one compared.
    subscription_event_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    projects: Mapped[list["Project"]] = relationship(back_populates="organization")
    members: Mapped[list["OrgMembership"]] = relationship(back_populates="organization")


class OrgMembership(Base):
    """A user's seat in an organization, with the role that applies to all of its workspaces."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_membership_org_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="MEMBER")  # OWNER | ADMIN | MEMBER
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_projects_source_external"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    # The account this workspace belongs to (migration 0023). Everyone who can reach this project
    # reaches it through an OrgMembership here. NULL only for projects with no human owner at all
    # (CLI-seeded, dev mode): unreachable by session auth, usable by ingest key, quota-counted on
    # its own.
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # tenancy source: "local" (self-host workspace) or "clerk" (org/personal provisioned from Clerk)
    source: Mapped[str] = mapped_column(String(16), default="local")
    # Clerk org_id, or "user:<clerk_user_id>" for a personal workspace; NULL for local single-workspace
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # This workspace's own OpenRouter key (Fernet-encrypted, see infrastructure/llm/provider.py),
    # used for every LLM eval call instead of the server-wide OPENROUTER_API_KEY. NULL = server key.
    openrouter_api_key_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Hosted-cloud billing (migration 0021). `free | pro | unlimited` — `unlimited` is for
    # operator workspaces (set via SQL) and is never written by webhooks. Both defaults (Python +
    # server) so none of the Project-creation sites need to name the column.
    # Legacy billing columns (migrations 0021/0022), superseded by the same fields on
    # Organization. No code reads them any more; kept one release so a rollback still finds its
    # data, dropped in a later cleanup migration.
    plan: Mapped[str] = mapped_column(String(16), default="free", server_default="free")
    billing_owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped[Organization | None] = relationship(back_populates="projects")
    ingest_keys: Mapped[list["IngestKey"]] = relationship(back_populates="project")
    agents: Mapped[list["Agent"]] = relationship(back_populates="project")
    memberships: Mapped[list["Membership"]] = relationship(back_populates="project")


class UsageCounter(Base):
    """One row per (project, UTC calendar month): how many externally-ingested traces landed.

    Written by the worker's counting hook (`services/quota_service.py`), read by the OTLP edge
    gate and the billing usage endpoint. Deliberately NOT part of the project data wipe — the
    wipe clears trace-derived product data, and clearing this would make it a self-serve monthly
    quota reset.
    """

    __tablename__ = "usage_counters"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM", UTC
    traces: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestKey(Base):
    __tablename__ = "ingest_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="ingest_keys")


class User(Base):
    """A human identity. In local mode, `password_hash` is set (argon2). In clerk mode the user is
    upserted from a verified Clerk JWT (`external_id` = Clerk user id, `password_hash` NULL).
    Email/external_id are unique *per source* so the two backends never collide."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_users_source_external"),
        # email is unique only among local accounts (Clerk emails may be unknown/empty/duplicated)
        Index(
            "uq_users_local_email",
            "email",
            unique=True,
            postgresql_where=text("source = 'local'"),
            sqlite_where=text("source = 'local'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    source: Mapped[str] = mapped_column(String(16), default="local")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Stamped into every session token as `tv` and checked on each request; bumping it ends every
    # session issued before now (see `auth/tokens.py`). Sessions are stateless, so this counter is
    # the only thing that can revoke one.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")


class Membership(Base):
    """LEGACY per-project membership, superseded by `OrgMembership` (migration 0023).

    Access is now derived from the organization, so nothing reads or writes this table. Its rows
    are kept for one release as the rollback path for the 0023 backfill; a later migration drops
    it."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="MEMBER")  # OWNER | ADMIN | MEMBER
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="memberships")
    project: Mapped[Project] = relationship(back_populates="memberships")


class PasswordReset(Base):
    """A single-use password-reset grant (local mode only; Clerk owns resets in hosted mode).

    Mirrors `Invitation`: only the sha256 of the raw token is stored, so a database dump cannot be
    replayed into account takeover. Rows are consumed (`used_at`) rather than deleted, so a
    reused link is rejected loudly instead of silently minting a second reset.
    """

    __tablename__ = "password_resets"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_password_resets_token_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invitation(Base):
    """A pending invite to join an ORGANIZATION (local mode only; Clerk owns invites in hosted
    mode). Only the sha256 of the raw token is stored; the raw token is shown once at creation.

    Accepting grants an `OrgMembership`, i.e. access to every workspace in that org — there is no
    way to invite someone into a single workspace, which is what keeps tenants from bleeding into
    each other."""

    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_invitations_token_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Legacy: what invites targeted before 0023. NULL on every invite created since.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(16), default="MEMBER")
    token_hash: Mapped[str] = mapped_column(String(64))
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|ACCEPTED|REVOKED|EXPIRED
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_agent_project_slug"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    slug: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(32), default=AgentKind.SINGLE.value)
    role: Mapped[str] = mapped_column(String(32), default=AgentRole.GENERIC.value)
    framework: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="agents")
    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent")


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "config_hash", name="uq_agentversion_agent_confighash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(256), default="")
    git_sha: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped[Agent] = relationship(back_populates="versions")


class EvaluationSuite(Base):
    __tablename__ = "evaluation_suites"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", "slug", name="uq_suite_project_agent_slug"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(32), default="REGRESSION")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", "input_digest", name="uq_case_project_agent_inputdigest"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    level: Mapped[str] = mapped_column(String(32), default="AGENT_RUN")
    title: Mapped[str] = mapped_column(String(512), default="")
    input_digest: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    origin: Mapped[str] = mapped_column(String(32), default="MANUAL")
    source_trace_id: Mapped[str] = mapped_column(String(64), default="")
    source_span_id: Mapped[str] = mapped_column(String(64), default="")
    agent_version_first_failed: Mapped[str | None] = mapped_column(String(36), nullable=True)
    fixture_bundle_s3_key: Mapped[str] = mapped_column(String(512), default="")
    reference_trajectory: Mapped[dict] = mapped_column(JSON, default=dict)
    assertions: Mapped[dict] = mapped_column(JSON, default=dict)
    match_mode: Mapped[str] = mapped_column(String(16), default="superset")
    fail_to_pass_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(128), default="ui")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationSuiteCase(Base):
    __tablename__ = "evaluation_suite_cases"

    suite_id: Mapped[str] = mapped_column(ForeignKey("evaluation_suites.id"), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), primary_key=True)
    pinned_case_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseReplay(Base):
    __tablename__ = "case_replays"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), index=True)
    candidate_trace_id: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(8))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GateRun(Base):
    __tablename__ = "gate_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    env: Mapped[str] = mapped_column(String(16), default="ci")
    git_ref: Mapped[str] = mapped_column(String(80), default="")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GateCase(Base):
    """One graded unit inside a gate run — either a promoted regression case replayed against a
    candidate trace, or an emulated conversation driven against the agent's endpoint. Exactly one
    of `evaluation_case_id` / `scenario_id` is set; for a scenario, `candidate_trace_id` holds the
    conversation (thread) id and `detail["trace_ids"]` the per-turn traces."""

    __tablename__ = "gate_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gate_run_id: Mapped[str] = mapped_column(ForeignKey("gate_runs.id"), index=True)
    evaluation_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("evaluation_cases.id"), nullable=True
    )
    scenario_id: Mapped[str | None] = mapped_column(ForeignKey("scenarios.id"), nullable=True)
    candidate_trace_id: Mapped[str] = mapped_column(String(64), default="")
    verdict: Mapped[str] = mapped_column(String(12))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentEndpoint(Base):
    """How Tracely reaches a customer's agent over HTTP to drive an emulated conversation.

    One row per agent. The bearer token is Fernet-encrypted at rest with the same
    `SECRETS_ENCRYPTION_KEY` machinery as a workspace's own OpenRouter key — it is a customer
    credential for a system we call out to, and must never be readable from a DB dump or echoed
    back by the API.
    """

    __tablename__ = "agent_endpoints"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    auth_header: Mapped[str] = mapped_column(String(64), default="Authorization")
    auth_scheme: Mapped[str] = mapped_column(String(32), default="Bearer")
    token_encrypted: Mapped[str] = mapped_column(String(2000), default="")
    extra_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    # Merged into every request body — the fields a real API needs alongside the message
    # (tenant_id, user_id, locale, channel). Query params need no equivalent: they ride along in
    # `url`, which is posted verbatim.
    extra_body: Mapped[dict] = mapped_column(JSON, default=dict)
    # Dotted path to the assistant's text in the response body. Empty = try the common shapes
    # (see SimulationService._extract_reply), which covers OpenAI-compatible and most bespoke APIs.
    reply_path: Mapped[str] = mapped_column(String(200), default="")
    # Body key carrying the session id, so the agent keeps server-side state across the turns of
    # one scenario.
    session_key: Mapped[str] = mapped_column(String(120), default="conversation_id")
    # Dotted path to a session id the endpoint MINTS in its response (e.g. `session_id`).
    #
    # Two conventions exist and they are opposites. Most APIs accept a client-supplied id: we send
    # ours under `session_key` on every turn and they key their state off it — that is the default,
    # `session_path` empty. Others own the identity: turn 1 carries no session at all, the response
    # names one, and every later turn must echo THAT value back. Setting this switches to the
    # second: turn 1 omits `session_key`, and turns 2..N send what the endpoint returned. Without
    # it such an endpoint starts a brand-new conversation on every turn and the scenario grades
    # three disconnected greetings.
    session_path: Mapped[str] = mapped_column(String(200), default="")
    timeout_s: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Scenario(Base):
    """A multi-turn conversation to run against the agent's endpoint.

    `SCRIPTED` replays `turns` verbatim — authored by hand, or imported from a real production
    thread (`source_thread_id`), which is the interesting case: the conversation that broke in
    production becomes the conversation that gates the PR claiming to fix it.

    `ADVERSARIAL` has no fixed turns. An attacker LLM is given `goal` and improvises up to
    `max_turns`, so the suite probes for the failure instead of re-checking a known one.
    """

    __tablename__ = "scenarios"
    __table_args__ = (Index("ix_scenario_project_agent", "project_id", "agent_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    kind: Mapped[str] = mapped_column(String(16), default="SCRIPTED")
    turns: Mapped[list] = mapped_column(JSON, default=list)
    goal: Mapped[str] = mapped_column(Text, default="")
    max_turns: Mapped[int] = mapped_column(Integer, default=6)
    # ADVERSARIAL only: the OpenRouter model id the attacker role-plays with. Blank = the server's
    # `attacker_model` (then the default judge model). SCRIPTED replays fixed turns, so it has no
    # attacker to pick a model for.
    attacker_model: Mapped[str] = mapped_column(String(120), default="")
    source_thread_id: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(128), default="ui")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalChainProgress(Base):
    """How far a sequential evaluator's durable conversation has advanced through a thread.

    `turn_ids` is the ordered list of traces already graded onto the column's conversation;
    `last_payload` is the chained context the NEXT turn is seeded with (`CFG_PREVIOUS`). The
    settled-thread pass uses this to grade only new turns; a stored prefix that stops matching
    the thread's turn order forces a rebuild from turn 1 (see `EvaluationService.evaluate_thread`).
    """

    __tablename__ = "eval_chain_progress"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    score_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    turn_ids: Mapped[list] = mapped_column(JSON, default=list)
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Evaluator(Base):
    """A user-configured online evaluator. The runner loads the project's enabled rows and runs
    them on each trace (filtered by agent/env, sampled). The built-in checks are seeded as editable
    records, not hardcoded defaults."""

    __tablename__ = "evaluators"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(400), default="")
    kind: Mapped[str] = mapped_column(String(16))
    score_name: Mapped[str] = mapped_column(String(80))
    level: Mapped[str] = mapped_column(String(16), default="AGENT_RUN")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    target_agent: Mapped[str] = mapped_column(String(80), default="")
    target_env: Mapped[str] = mapped_column(String(32), default="")
    sampling: Mapped[float] = mapped_column(Float, default=1.0)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FailureCluster(Base):
    __tablename__ = "failure_clusters"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", "cluster_key", name="uq_cluster_project_agent_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    cluster_key: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(256), default="")
    taxonomy: Mapped[str] = mapped_column(String(64), default="")
    signature: Mapped[str] = mapped_column(String(2000), default="")
    description: Mapped[str] = mapped_column(String(4000), default="")
    proposed_fix: Mapped[str] = mapped_column(String(4000), default="")
    severity: Mapped[str] = mapped_column(String(16), default="")
    method: Mapped[str] = mapped_column(String(16), default="signature")
    count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    candidate_case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    cluster_id: Mapped[str] = mapped_column(ForeignKey("failure_clusters.id"), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_medoid: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(String(1000), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FailureEmbedding(Base):
    """Cached embedding of a failing run's text (for batch UMAP+HDBSCAN clustering)."""

    __tablename__ = "failure_embeddings"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    summary: Mapped[str] = mapped_column(String(4000), default="")
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MetaAnalysis(Base):
    """A cross-metric meta-analysis over an agent's evaluator scores: Spearman correlations +
    z-score outliers (computed deterministically in Python) plus an LLM-written synthesis
    (patterns / recommendations / summary). Scoped per (project, agent); `agent_id` is the events
    agent id (Agent uuid) the analysis covered, or "" for a whole-project analysis. `result` holds
    the full `MetaAnalysisOutput`; `meta` holds run provenance (model, counts, agent slug)."""

    __tablename__ = "meta_analyses"
    __table_args__ = (Index("ix_meta_analyses_project_agent", "project_id", "agent_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    analysis_type: Mapped[str] = mapped_column(String(32), default="agent")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RollingSummary(Base):
    """A per-span ACCUMULATING summary of a conversation: one row per span (step), each holding the
    full compressed summary of every step from the start of the thread up to and including it. The
    last row (highest `step_order`) is the whole-conversation summary. Backs the `@HISTORY` /
    conversation-judge context as a cache (stored compressed history instead of re-sending the raw
    transcript). Generation is idempotent — one row per (project, span)."""

    __tablename__ = "rolling_summaries"
    __table_args__ = (
        UniqueConstraint("project_id", "span_id", name="uq_rolling_summary_project_span"),
        Index("ix_rolling_summaries_thread", "project_id", "thread_id", "step_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    span_id: Mapped[str] = mapped_column(String(64))
    step_order: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[list] = mapped_column(JSON, default=list)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationAgent(Base):
    """The user-declared agent/tool catalog for a conversation, sent via the SDK
    (`tracely.trace(..., agents=[...])`) as a `tracely.agents` span attribute and captured at ingest.
    One row per (project, thread); `agents` is the declared list
    `[{name, description, tools: {tool_name: {name, description, parameters}}}]`. Distinct from the
    `agents` REGISTRY table (those are observed agent ids); this is optional, richer, user-supplied
    metadata surfaced in the Conversation Agents panel and `@LIST_AGENT`."""

    __tablename__ = "conversation_agents"
    __table_args__ = (
        UniqueConstraint("project_id", "thread_id", name="uq_conversation_agents_project_thread"),
        Index("ix_conversation_agents_project_thread", "project_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(64))
    agents: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScoreAnnotation(Base):
    """A human label on an evaluator's verdict — the judge-vs-human calibration write path. A reviewer
    agrees/disagrees with a judge score on a target (trace / span / thread); we snapshot the judge
    verdict at label time so agreement is a pure Postgres query. Keyed by the score's natural
    identity (matches the ClickHouse `scores` natural key) + the labeler — one label per user per
    score, upserted."""

    __tablename__ = "score_annotations"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "score_name", "evaluation_level", "trace_id", "session_id",
            "observation_id", "labeled_by", name="uq_score_annotations_target_labeler",
        ),
        Index("ix_score_annotations_project_name", "project_id", "score_name"),
        Index("ix_score_annotations_project_trace", "project_id", "trace_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    score_name: Mapped[str] = mapped_column(String(128))
    evaluation_level: Mapped[str] = mapped_column(String(32), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    session_id: Mapped[str] = mapped_column(String(128), default="")
    observation_id: Mapped[str] = mapped_column(String(64), default="")
    judge_verdict: Mapped[str] = mapped_column(String(32), default="")  # snapshot at label time
    human_verdict: Mapped[str] = mapped_column(String(32))  # PASS | FAIL | …
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    labeled_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssistantChat(Base):
    """One conversation with the in-app assistant, owned by the person who had it.

    The whole transcript is a single JSON column: a chat is always read and written whole, never
    queried message-by-message, so a second table would buy nothing but joins. `messages` is a
    list of `{role, content, attachments?, ts}`; each attachment is `{id, name, mime, size}` and
    its bytes live in object storage under the project's prefix (so a workspace delete takes
    them with it).

    `user_id` is NULL for machine callers (an ingest key has no human identity) and in dev mode —
    those share the project's chats, which is the only sensible reading of "whose is this" when
    nobody signed in.
    """

    __tablename__ = "assistant_chats"
    __table_args__ = (Index("ix_assistant_chats_owner", "project_id", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    messages: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Monitor(Base):
    """A threshold rule over the regression-loop metrics already in ClickHouse — when its
    `condition` fires over a sliding window, it POSTs to each configured `channel`. Per-monitor
    `min_interval_seconds` dedupes alerts so a noisy condition doesn't page every minute.

    `condition` shape (JSON) — the engine dispatches on `type`:
      `fail_rate_over` — `{score_name, window_minutes, min_samples, threshold}` — fraction of
        FAIL verdicts on a given evaluator over the window must stay BELOW threshold.
      `score_below`    — `{score_name, window_minutes, min_samples, threshold}` — average
        numeric `value` over the window must stay AT OR ABOVE threshold.
      `trace_failure_rate` — `{window_minutes, min_samples, threshold}` — overall failing-trace
        rate (advisory FAILs excluded) over the window must stay BELOW threshold.

    `channels` (JSON list): `[{type: 'slack', url}, {type: 'webhook', url, headers?}]` — the
    simple action, used when the monitor has no `steps`. With steps, the action is a **flow**:
    an ordered DAG of `MonitorStep`s whose graph lives in `flow_layout` (React Flow's own
    `{nodes, edges}` JSON, read by the engine itself — see `domain/alerting/flow.py`)."""

    __tablename__ = "monitors"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_monitors_project_name"),
        Index("ix_monitors_project_enabled", "project_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(400), default="")
    target_agent: Mapped[str] = mapped_column(String(80), default="")
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_summary: Mapped[str] = mapped_column(String(500), default="")
    # React Flow's `{nodes, edges}`, stored opaquely. `edges` is the only part the engine reads.
    flow_layout: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    steps: Mapped[list["MonitorStep"]] = relationship(
        back_populates="monitor",
        cascade="all, delete-orphan",
        order_by="MonitorStep.order_index",
        lazy="selectin",
    )


class MonitorStep(Base):
    """One executable step of a monitor's flow. `id` doubles as the canvas node id, so an edge in
    `Monitor.flow_layout` points straight at this row — no id translation on save.

    `config` shape per `step_type` (every string field is a Jinja template):
      `condition`         — `{expression}`; falsy short-circuits the whole run to `skipped`.
      `webhook`           — `{url, method, headers: [{key, value}], body_template}`.
      `slack`             — `{url, text_template}`.
      `send_email`        — `{to_template, subject_template, body_template, body_is_html}`.
      `llm_prompt`        — `{model, system_prompt, user_prompt_template, temperature,
                             output_schema: [{name, type, description}]}`.
      `python_expression` — `{expression}`, one allowlisted expression (no `eval`).
    """

    __tablename__ = "monitor_steps"
    __table_args__ = (Index("ix_monitor_steps_monitor_order", "monitor_id", "order_index"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    monitor_id: Mapped[str] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(120))
    step_type: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    monitor: Mapped["Monitor"] = relationship(back_populates="steps")


class MonitorExecution(Base):
    """One run of a monitor's flow. `step_results` is the audit trail — per step: the result, the
    error, the ancestors whose outputs it could read, and `rendered_config`, the POST-Jinja value
    of every field actually sent. That last one is what makes a run self-explanatory: you see that
    `{{ trace.url }}` resolved to a real link without re-running anything."""

    __tablename__ = "monitor_executions"
    __table_args__ = (
        Index("ix_monitor_executions_monitor_started", "monitor_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    monitor_id: Mapped[str] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    trigger_type: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)


class ShareRevocation(Base):
    """The kill switch for public share links.

    Share tokens are stateless signed capabilities, so there is nothing to delete — revoking means
    recording *when* an owner said stop, and refusing every token issued before that. One row per
    shared subject (not per token): "stop sharing this gate" has to kill every link ever minted for
    it, including ones the owner no longer has a copy of. Re-sharing afterwards just mints a token
    with a later `iat`, which passes again.
    """

    __tablename__ = "share_revocations"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
