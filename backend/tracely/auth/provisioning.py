"""Organization + workspace + identity provisioning for the auth flows.

Every account is an Organization: `personal` (one human, 1 workspace, un-joinable) or `company`
(a team, several workspaces and seats). Users are members of the org, and a workspace is
reachable exactly when the caller belongs to its org — see `principal.select_membership`.

Two registration shapes, chosen by `ALLOW_PUBLIC_SIGNUP`:
  - self-host (default): the first registrant claims the deployment, gets a COMPANY org (so they
    can invite the team) and adopts the seeded "default" project; everyone else joins by invite.
  - hosted cloud: anyone may sign up and gets their own PERSONAL org + workspace.

The Clerk path lives in `auth/clerk.py` and calls `upsert_clerk_principal` here; a Clerk org maps
to a company org, a Clerk personal account to a personal one."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tracely.auth.invitations import hash_token
from tracely.auth.principal import AuthError, Principal
from tracely.config import settings
from tracely.domain.billing import (
    KIND_COMPANY,
    KIND_PERSONAL,
    seat_limit_for,
    workspace_limit_for,
)
from tracely.domain.evaluation.evaluators import TEMPLATES
from tracely.infrastructure.db.models import (
    Evaluator,
    IngestKey,
    Invitation,
    Organization,
    OrgMembership,
    Project,
    User,
)


def new_ingest_key() -> str:
    """Opaque, dot-free ingest key — the classifier must never mistake it for a JWT."""
    return "tk_" + secrets.token_urlsafe(32)


async def seed_recommended_evaluators(session: AsyncSession, project_id: str) -> int:
    """Install the recommended evaluator catalog as editable records (idempotent by score_name)
    so online evaluation + the grid's metric columns work out of the box for every new
    workspace. Async twin of services/seeding_service._seed_evaluators (the CLI seeder)."""
    existing = set(
        (
            await session.execute(
                select(Evaluator.score_name).where(Evaluator.project_id == project_id)
            )
        ).scalars()
    )
    added = 0
    for t in TEMPLATES:
        if not t.get("recommended") or t["score_name"] in existing:
            continue
        session.add(Evaluator(
            id=str(uuid4()), project_id=project_id, name=t["name"],
            description=t.get("description", ""), kind=t["kind"], score_name=t["score_name"],
            level=t["level"], config=t.get("config") or {},
        ))
        added += 1
    return added


def _as_utc(dt: datetime) -> datetime:
    # SQLite (tests) returns naive datetimes from DateTime(timezone=True); treat them as UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _get_or_create(session: AsyncSession, model, *, where, defaults):
    """Portable, race-safe get-or-create (works on Postgres + SQLite). A concurrent insert that loses
    the unique-constraint race is caught at the savepoint and resolved by re-selecting the winner.
    Returns `(obj, created)` so callers can run one-time provisioning (e.g. evaluator seeding)."""
    obj = (await session.execute(select(model).where(*where))).scalar_one_or_none()
    if obj is not None:
        return obj, False
    try:
        async with session.begin_nested():
            obj = model(**defaults)
            session.add(obj)
        return obj, True
    except IntegrityError:
        return (await session.execute(select(model).where(*where))).scalar_one(), False


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (base or "workspace")[:96]


# ── organizations ─────────────────────────────────────────────────────────────

async def workspace_count(session: AsyncSession, organization_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Project)
            .where(Project.organization_id == organization_id)
        )
    ).scalar_one()


async def seat_count(session: AsyncSession, organization_id: str) -> int:
    """Members plus outstanding invites — a pending invite is a seat already spoken for, or an
    org could invite past its cap and only discover it when people accept."""
    members = (
        await session.execute(
            select(func.count())
            .select_from(OrgMembership)
            .where(OrgMembership.organization_id == organization_id)
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(func.count())
            .select_from(Invitation)
            .where(
                Invitation.organization_id == organization_id,
                Invitation.status == "PENDING",
            )
        )
    ).scalar_one()
    return int(members) + int(pending)


async def assert_can_add_workspace(session: AsyncSession, org: Organization) -> None:
    """Raises 409 when the org is at its workspace cap. Caps only exist on the hosted plans —
    a self-hosted deployment (BILLING_ENABLED off) is never limited."""
    if not settings.billing_enabled:
        return
    limit = workspace_limit_for(
        org.plan, org.kind, settings.free_workspace_limit, settings.pro_workspace_limit
    )
    if limit is None:
        return
    if await workspace_count(session, org.id) >= limit:
        if org.kind == KIND_PERSONAL:
            raise AuthError(
                409,
                "a personal account holds one workspace — create an organization to add more",
            )
        raise AuthError(
            409,
            f"this organization is at its workspace limit ({limit}) — upgrade to add more",
        )


async def assert_can_add_seat(session: AsyncSession, org: Organization) -> None:
    """Raises 409 when the org has no seat left for another member or pending invite."""
    if org.kind == KIND_PERSONAL:
        # Not a billing limit: a personal account is one human by definition, so it stays
        # un-joinable even on a self-hosted deployment where nothing else is capped.
        raise AuthError(
            409, "a personal account can't have teammates — create an organization to invite"
        )
    if not settings.billing_enabled:
        return
    limit = seat_limit_for(
        org.plan, org.kind, settings.free_seat_limit, settings.pro_seat_limit
    )
    if limit is None:
        return
    if await seat_count(session, org.id) >= limit:
        raise AuthError(
            409, f"this organization is at its seat limit ({limit}) — upgrade to invite more"
        )


async def company_orgs_joined(session: AsyncSession, user_id: str) -> int:
    """Company orgs the user BELONGS to, whatever their role. Membership is what counts, not
    ownership: someone already working inside a company doesn't get to spin up their own on the
    side — that would be a second free quota pool per person, whoever created it."""
    return (
        await session.execute(
            select(func.count())
            .select_from(OrgMembership)
            .join(Organization, Organization.id == OrgMembership.organization_id)
            .where(
                OrgMembership.user_id == user_id,
                Organization.kind == KIND_COMPANY,
            )
        )
    ).scalar_one()


async def can_create_organization(session: AsyncSession, user_id: str | None) -> bool:
    """Whether this user may create another company org. Uncapped when billing is off."""
    if not user_id:
        return False
    if not settings.billing_enabled:
        return True
    # No plan lifts this. The plan belongs to the ORG and buys workspaces, seats and quota
    # inside it — a bigger account is a bigger org, never a second one.
    return (
        await company_orgs_joined(session, user_id) < settings.max_organizations_per_user
    )


async def assert_can_create_organization(session: AsyncSession, user_id: str) -> None:
    if not await can_create_organization(session, user_id):
        raise AuthError(
            409,
            "you already belong to an organization — add workspaces to it instead of creating "
            "another",
        )


async def create_organization(
    session: AsyncSession, *, name: str, kind: str, owner_user_id: str
) -> Organization:
    """A new org with `owner_user_id` as its OWNER. Does not create a workspace — callers that
    need one call `create_workspace` next (inside the same transaction)."""
    name = (name or "").strip() or ("Personal" if kind == KIND_PERSONAL else "Organization")
    org = Organization(
        id=str(uuid4()),
        name=name,
        slug=f"{_slugify(name)}-{secrets.token_hex(3)}",
        kind=kind,
    )
    session.add(org)
    await session.flush()
    session.add(
        OrgMembership(
            id=str(uuid4()), organization_id=org.id, user_id=owner_user_id, role="OWNER"
        )
    )
    return org


async def get_organization(session: AsyncSession, organization_id: str) -> Organization | None:
    return await session.get(Organization, organization_id)


async def user_organizations(
    session: AsyncSession, user_id: str
) -> list[tuple[Organization, str]]:
    rows = (
        await session.execute(
            select(Organization, OrgMembership.role)
            .join(OrgMembership, OrgMembership.organization_id == Organization.id)
            .where(OrgMembership.user_id == user_id)
            .order_by(Organization.created_at)
        )
    ).all()
    return [(o, role) for o, role in rows]


# ── local mode ────────────────────────────────────────────────────────────────

async def any_local_user(session: AsyncSession) -> bool:
    n = (
        await session.execute(
            select(func.count()).select_from(User).where(User.source == "local")
        )
    ).scalar_one()
    return bool(n)


async def get_singleton_local_project(session: AsyncSession) -> Project | None:
    return (
        await session.execute(
            select(Project)
            .where(Project.source == "local")
            .order_by(Project.created_at)
            .limit(1)
        )
    ).scalars().first()


async def _ensure_ingest_key(session: AsyncSession, project_id: str) -> None:
    existing = (
        await session.execute(
            select(IngestKey).where(IngestKey.project_id == project_id).limit(1)
        )
    ).scalars().first()
    if not existing:
        session.add(IngestKey(id=str(uuid4()), project_id=project_id, key=new_ingest_key()))


def _new_local_user(email: str, password_hash: str, display_name: str) -> User:
    return User(
        id=str(uuid4()),
        email=email,
        source="local",
        password_hash=password_hash,
        display_name=display_name,
    )


async def bootstrap_owner(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str = "",
    workspace_name: str = "Tracely",
) -> tuple[Project, User]:
    """Self-host first-run: make `email` the OWNER of a COMPANY org holding the singleton local
    workspace (reusing the seeded project if one exists). Company, not personal, because the
    point of a self-hosted deployment is that the owner invites their team into it."""
    user = _new_local_user(email, password_hash, display_name)
    session.add(user)
    await session.flush()
    org = await create_organization(
        session, name=workspace_name, kind=KIND_COMPANY, owner_user_id=user.id
    )
    project = await get_singleton_local_project(session)
    if project is None:
        project = Project(id=str(uuid4()), slug="default", name=workspace_name, source="local")
        session.add(project)
        await session.flush()
    project.organization_id = org.id
    await _ensure_ingest_key(session, project.id)
    await seed_recommended_evaluators(session, project.id)
    await session.commit()
    return project, user


async def create_personal_org(
    session: AsyncSession,
    *,
    user_id: str,
    name: str,
    workspace_name: str = "",
) -> Project:
    """A personal org with exactly one workspace, owned by `user_id`.

    Two callers, one reason: this is the floor of the account model — somewhere that is yours
    alone and that nobody can take away. Public signup starts you here, and leaving your only
    company org drops you back here rather than being refused.

    Deliberately not behind `assert_can_create_organization`: that cap counts COMPANY orgs, and
    this org is empty by construction (no seats, no traces, free plan)."""
    org = await create_organization(
        session, name=name, kind=KIND_PERSONAL, owner_user_id=user_id
    )
    project, _key = await create_workspace(
        session, name=(workspace_name or "My workspace"), organization_id=org.id
    )
    return project


async def signup_personal(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str = "",
    workspace_name: str = "",
) -> tuple[Project, User]:
    """Hosted public signup: a personal org with exactly one workspace. Never adopts the seeded
    project — every signup is its own tenant."""
    user = _new_local_user(email, password_hash, display_name)
    session.add(user)
    await session.flush()
    project = await create_personal_org(
        session,
        user_id=user.id,
        name=(display_name or email.split("@")[0]),
        workspace_name=workspace_name,
    )
    return project, user


async def create_workspace(
    session: AsyncSession, *, name: str, organization_id: str
) -> tuple[Project, IngestKey]:
    """Create a workspace inside an org, with its own ingest key. Backs the UI's "New workspace"
    action: any member of the org can then switch to it (X-Tracely-Project) and push traces with
    the returned key. The slug gets a short random suffix so same-named workspaces never collide
    on the unique constraint.

    Callers that act on a user's request must call `assert_can_add_workspace` first — this
    function is also used by signup, where the org is empty by construction."""
    name = (name or "").strip() or "Workspace"
    project = Project(
        id=str(uuid4()),
        slug=f"{_slugify(name)}-{secrets.token_hex(3)}",
        name=name,
        source="local",
        organization_id=organization_id,
    )
    session.add(project)
    await session.flush()
    key = IngestKey(id=str(uuid4()), project_id=project.id, key=new_ingest_key())
    session.add(key)
    await seed_recommended_evaluators(session, project.id)
    await session.commit()
    # Fill it with the demo dataset so it opens on a working product rather than empty pages.
    # Detached and best-effort — after the commit, so a seeding hiccup can't undo the workspace.
    if settings.seed_new_workspaces:
        from tracely.services import demo_seed  # lazy: keeps subprocess/pathing out of import time

        demo_seed.launch(key.key)
    return project, key


async def create_invitation(
    session: AsyncSession,
    *,
    organization_id: str,
    email: str,
    role: str,
    invited_by: str | None,
    token_hash: str,
    ttl_seconds: int = 7 * 24 * 3600,
) -> Invitation:
    """Invite someone into an ORG (never a single workspace). The caller has already checked the
    seat cap via `assert_can_add_seat`."""
    email = email.lower().strip()
    already = (
        await session.execute(
            select(func.count())
            .select_from(OrgMembership)
            .join(User, User.id == OrgMembership.user_id)
            .where(
                OrgMembership.organization_id == organization_id,
                User.email == email,
                User.source == "local",
            )
        )
    ).scalar_one()
    if already:
        raise AuthError(409, "that person is already a member of this organization")
    inv = Invitation(
        id=str(uuid4()),
        organization_id=organization_id,
        email=email,
        role=role,
        token_hash=token_hash,
        invited_by=invited_by,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    session.add(inv)
    await session.commit()
    return inv


async def accept_invitation(
    session: AsyncSession,
    *,
    raw_token: str,
    password: str,
    display_name: str = "",
) -> tuple[User, Project]:
    """Atomically consume a valid invite and create (or re-attach) the member. Raises AuthError.

    Takes the raw password, not a hash, because of the re-attach case: the invite token is shown to
    the INVITER, so when the invited email already has an account, accepting must prove knowledge
    of that account's password — otherwise inviting someone is a way to log in as them, with every
    workspace they can reach. That check happens BEFORE the token is consumed, so a wrong guess
    leaves the invite pending for the real person."""
    from tracely.auth import passwords  # lazy: argon2 stays out of the clerk/dev import path

    inv = (
        await session.execute(
            select(Invitation).where(Invitation.token_hash == hash_token(raw_token))
        )
    ).scalar_one_or_none()
    if not inv or inv.status != "PENDING":
        raise AuthError(400, "invalid or used invitation")
    if _as_utc(inv.expires_at) <= datetime.now(timezone.utc):
        raise AuthError(400, "invitation expired")
    user = (
        await session.execute(
            select(User).where(User.source == "local", User.email == inv.email)
        )
    ).scalar_one_or_none()
    if user is not None and not passwords.verify_password(password, user.password_hash):
        raise AuthError(401, "that email already has an account — accept with its password")
    # single-use: flip PENDING -> ACCEPTED, asserting we won any race
    res = await session.execute(
        update(Invitation)
        .where(Invitation.id == inv.id, Invitation.status == "PENDING")
        .values(status="ACCEPTED", accepted_at=datetime.now(timezone.utc))
    )
    if res.rowcount != 1:
        raise AuthError(400, "invalid or used invitation")
    if not inv.organization_id:
        raise AuthError(400, "invalid or used invitation")
    if user is None:
        user = _new_local_user(inv.email, passwords.hash_password(password), display_name)
        session.add(user)
        await session.flush()
    existing = (
        await session.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == user.id,
                OrgMembership.organization_id == inv.organization_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            OrgMembership(
                id=str(uuid4()),
                user_id=user.id,
                organization_id=inv.organization_id,
                role=inv.role,
            )
        )
    # Land the new member in one of the org's workspaces (the oldest) so their session has an
    # active project — an org always has at least one.
    project = (
        await session.execute(
            select(Project)
            .where(Project.organization_id == inv.organization_id)
            .order_by(Project.created_at, Project.id)
            .limit(1)
        )
    ).scalars().first()
    if project is None:
        raise AuthError(400, "this organization has no workspace yet")
    await session.commit()
    return user, project


# ── clerk mode ────────────────────────────────────────────────────────────────

async def upsert_clerk_principal(
    session: AsyncSession,
    *,
    clerk_user_id: str,
    email: str,
    display_name: str,
    org_id: str | None,
    role: str,
) -> Principal:
    """Idempotently upsert User + Organization + Project (+ IngestKey) + OrgMembership from
    verified Clerk claims. Concurrent first-requests can't create duplicates (unique constraints
    + race-safe get-or-create)."""
    external_project = org_id or f"user:{clerk_user_id}"

    user, _ = await _get_or_create(
        session,
        User,
        where=(User.source == "clerk", User.external_id == clerk_user_id),
        defaults=dict(
            id=str(uuid4()),
            email=email,
            source="clerk",
            external_id=clerk_user_id,
            display_name=display_name,
        ),
    )

    # A Clerk org is a company account; a Clerk personal account is a personal one. Keying the
    # org on the Clerk id (its unique slug) is what makes this idempotent under concurrency.
    org, _ = await _get_or_create(
        session,
        Organization,
        where=(Organization.slug == f"clerk-{external_project}"[:128],),
        defaults=dict(
            id=str(uuid4()),
            name=(f"Org {org_id}" if org_id else (email or "Personal")),
            slug=f"clerk-{external_project}"[:128],
            kind=(KIND_COMPANY if org_id else KIND_PERSONAL),
        ),
    )

    # the workspace — one per Clerk org / personal account
    project, project_created = await _get_or_create(
        session,
        Project,
        where=(Project.source == "clerk", Project.external_id == external_project),
        defaults=dict(
            id=str(uuid4()),
            slug=f"clerk-{external_project}"[:128],
            name=(f"Org {org_id}" if org_id else email),
            source="clerk",
            external_id=external_project,
            organization_id=org.id,
        ),
    )
    if project.organization_id is None:  # a pre-0023 row meeting the org layer for the first time
        project.organization_id = org.id
    await _ensure_ingest_key(session, project.id)
    if project_created:
        await seed_recommended_evaluators(session, project.id)

    # membership — role synced from Clerk on every request (Clerk is source of truth)
    membership, _ = await _get_or_create(
        session,
        OrgMembership,
        where=(OrgMembership.user_id == user.id, OrgMembership.organization_id == org.id),
        defaults=dict(id=str(uuid4()), user_id=user.id, organization_id=org.id, role=role),
    )
    if membership.role != role:
        membership.role = role

    await session.commit()
    return Principal(
        project_id=project.id, user_id=user.id, role=role, kind="clerk", organization_id=org.id
    )
