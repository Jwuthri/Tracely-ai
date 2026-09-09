"""The organization tier: who can join what, and how big an account may get.

These pin the rules the layer exists for — break one and either tenants bleed into each other or
the free plan becomes unbounded:

- a workspace is reachable exactly when you belong to its ORG (no per-workspace grants);
- a personal account is one human with one workspace, and can never be joined;
- a company account's workspaces and seats are bounded by its plan, with pending invites
  counting as seats already taken;
- caps only exist when billing is on, so self-hosters stay unlimited;
- registration is invite-only unless the deployment opts into public signup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tracely.config import settings
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base


@pytest.fixture
def sync_db():
    """In-memory sync SQLite for the repository half of deletion."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng,
        tables=[
            models.Organization.__table__,
            models.OrgMembership.__table__,
            models.Invitation.__table__,
            models.Project.__table__,
            models.User.__table__,
        ],
    )
    yield sessionmaker(eng)
    eng.dispose()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def hosted(monkeypatch):
    """Hosted cloud: public signup + billing caps, with small limits so tests stay readable."""
    monkeypatch.setattr(settings, "allow_public_signup", True)
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_workspace_limit", 2)
    monkeypatch.setattr(settings, "free_seat_limit", 2)


async def _signup(client, email: str, password: str = "hunter2-pw") -> str:
    r = await client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ── registration shapes ───────────────────────────────────────────────────────


async def test_first_registrant_gets_a_company_org(client):
    """Self-host: the founder can invite their team, so their account is a company."""
    token = await _signup(client, "founder@x.test")
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    assert me["organization_kind"] == "company"
    assert me["role"] == "OWNER"
    assert len(me["projects"]) == 1


async def test_second_signup_is_invite_only_by_default(client):
    await _signup(client, "founder@x.test")
    r = await client.post(
        "/auth/register", json={"email": "stranger@x.test", "password": "hunter2-pw"}
    )
    assert r.status_code == 409  # an exposed self-host URL can't be used to mint accounts


async def test_public_signup_gives_each_account_its_own_personal_org(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "someone@x.test")
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    assert me["organization_kind"] == "personal"
    assert len(me["projects"]) == 1 and len(me["organizations"]) == 1


async def test_public_signup_rejects_a_duplicate_email(client, hosted):
    await _signup(client, "founder@x.test")
    await _signup(client, "dup@x.test")
    r = await client.post(
        "/auth/register", json={"email": "dup@x.test", "password": "other-pw-1"}
    )
    assert r.status_code == 409


# ── personal accounts are un-joinable and single-workspace ────────────────────


async def test_personal_account_cannot_add_a_second_workspace(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    r = await client.post("/auth/projects", json={"name": "Second"}, headers=_bearer(token))
    assert r.status_code == 409
    assert "organization" in r.json()["detail"]


async def test_personal_account_cannot_invite(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    r = await client.post(
        "/auth/invitations", json={"email": "friend@x.test"}, headers=_bearer(token)
    )
    assert r.status_code == 409
    assert "personal account" in r.json()["detail"]


async def test_creating_an_organization_is_how_a_solo_account_grows(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")

    r = await client.post("/auth/organizations", json={"name": "Acme"}, headers=_bearer(token))
    assert r.status_code == 200 and r.json()["kind"] == "company"
    org_id = r.json()["id"]

    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    assert len(me["organizations"]) == 2  # personal + the new company
    assert len([p for p in me["projects"] if p["organization_id"] == org_id]) == 1


async def test_joining_a_company_uses_up_your_one_organization(client, hosted):
    """Membership, not ownership: someone invited into a company can't also run their own on the
    side, which would be a second free pool for the same person."""
    owner = await _company_token(client)
    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test", "role": "ADMIN"},
        headers=_bearer(owner),
    )
    joined = await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    mh = _bearer(joined.json()["token"])

    me = (await client.get("/auth/me", headers=mh)).json()
    assert me["can_create_organization"] is False  # ADMIN of a company, owns nothing
    r = await client.post("/auth/organizations", json={"name": "Side"}, headers=mh)
    assert r.status_code == 409 and "already belong" in r.json()["detail"]


async def test_orgs_are_capped_or_the_whole_tier_is_theatre(client, hosted):
    """Each org is a fresh quota pool, so unlimited orgs would be unlimited free quota — the
    exact fan-out this layer exists to close, one level up."""
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")

    assert (
        await client.post("/auth/organizations", json={"name": "One"}, headers=_bearer(token))
    ).status_code == 200
    r = await client.post("/auth/organizations", json={"name": "Two"}, headers=_bearer(token))
    assert r.status_code == 409 and "organization" in r.json()["detail"]

    # and the menu stops offering it, rather than showing a button that always fails
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    assert me["can_create_organization"] is False


async def test_no_plan_buys_a_second_organization(client, hosted, session):
    """The plan belongs to the org and buys workspaces, seats and quota INSIDE it. Paying — even
    `unlimited` — must never turn into a second account."""
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    first = await client.post(
        "/auth/organizations", json={"name": "One"}, headers=_bearer(token)
    )
    for plan in ("pro", "unlimited"):
        org = await session.get(models.Organization, first.json()["id"])
        org.plan = plan
        await session.commit()
        r = await client.post(
            "/auth/organizations", json={"name": "Two"}, headers=_bearer(token)
        )
        assert r.status_code == 409, f"{plan} bought a second org"
        me = (await client.get("/auth/me", headers=_bearer(token))).json()
        assert me["can_create_organization"] is False


async def test_self_hosted_org_creation_is_uncapped(client, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", False)
    token = await _signup(client, "founder@x.test")
    for i in range(3):
        r = await client.post(
            "/auth/organizations", json={"name": f"Org {i}"}, headers=_bearer(token)
        )
        assert r.status_code == 200


# ── company caps ──────────────────────────────────────────────────────────────


async def _company_token(client, email="boss@x.test"):
    """A company org (the first registrant's) with its OWNER's token."""
    return await _signup(client, email)


async def test_workspace_cap_is_enforced_then_lifted_by_the_plan(client, hosted, session):
    token = await _company_token(client)
    # limit is 2 and signup created one
    assert (
        await client.post("/auth/projects", json={"name": "W2"}, headers=_bearer(token))
    ).status_code == 200
    r = await client.post("/auth/projects", json={"name": "W3"}, headers=_bearer(token))
    assert r.status_code == 409 and "workspace limit" in r.json()["detail"]

    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    org = await session.get(models.Organization, me["organization_id"])
    org.plan = "pro"
    await session.commit()

    assert (
        await client.post("/auth/projects", json={"name": "W3"}, headers=_bearer(token))
    ).status_code == 200


async def test_pending_invites_occupy_seats(client, hosted):
    """Otherwise an org invites past its cap and only discovers it when people accept."""
    token = await _company_token(client)
    # seat limit 2: the owner holds one, so exactly one invite fits
    r1 = await client.post(
        "/auth/invitations", json={"email": "a@x.test"}, headers=_bearer(token)
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/auth/invitations", json={"email": "b@x.test"}, headers=_bearer(token)
    )
    assert r2.status_code == 409 and "seat limit" in r2.json()["detail"]

    # revoking the pending invite frees the seat again
    await client.delete(f"/auth/invitations/{r1.json()['id']}", headers=_bearer(token))
    r3 = await client.post(
        "/auth/invitations", json={"email": "b@x.test"}, headers=_bearer(token)
    )
    assert r3.status_code == 200


async def test_self_hosted_deployments_are_never_capped(client, monkeypatch):
    """Billing off (the self-host default) = no workspace or seat limits at all."""
    monkeypatch.setattr(settings, "billing_enabled", False)
    token = await _company_token(client)
    for i in range(4):
        r = await client.post(
            "/auth/projects", json={"name": f"W{i}"}, headers=_bearer(token)
        )
        assert r.status_code == 200
    for i in range(4):
        r = await client.post(
            "/auth/invitations", json={"email": f"m{i}@x.test"}, headers=_bearer(token)
        )
        assert r.status_code == 200


# ── invites grant the whole org, and nothing outside it ───────────────────────


async def test_accepting_an_invite_grants_every_workspace_in_the_org(client, hosted):
    token = await _company_token(client)
    await client.post("/auth/projects", json={"name": "W2"}, headers=_bearer(token))
    owner_me = (await client.get("/auth/me", headers=_bearer(token))).json()

    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test"}, headers=_bearer(token)
    )
    joined = await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    assert joined.status_code == 200
    member_me = (
        await client.get("/auth/me", headers=_bearer(joined.json()["token"]))
    ).json()

    assert member_me["organization_id"] == owner_me["organization_id"]
    assert {p["id"] for p in member_me["projects"]} == {p["id"] for p in owner_me["projects"]}
    assert member_me["role"] == "MEMBER"


async def test_a_member_cannot_reach_another_accounts_workspace(client, hosted):
    """The cross-tenant boundary: no invite, no membership, no access — even by explicit id."""
    boss = await _company_token(client)
    boss_me = (await client.get("/auth/me", headers=_bearer(boss))).json()
    outsider = await _signup(client, "outsider@x.test")

    r = await client.get(
        "/auth/me",
        headers={**_bearer(outsider), "X-Tracely-Project": boss_me["project_id"]},
    )
    assert r.status_code == 403


async def test_members_endpoint_lists_the_org(client, hosted):
    token = await _company_token(client)
    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test", "role": "ADMIN"},
        headers=_bearer(token),
    )
    await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    rows = (await client.get("/auth/members", headers=_bearer(token))).json()
    assert {(r["email"], r["role"]) for r in rows} == {
        ("boss@x.test", "OWNER"),
        ("teammate@x.test", "ADMIN"),
    }


async def test_inviting_an_existing_member_is_refused(client, hosted):
    token = await _company_token(client)
    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test"}, headers=_bearer(token)
    )
    await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    r = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test"}, headers=_bearer(token)
    )
    assert r.status_code == 409


# ── deleting an organization ──────────────────────────────────────────────────


def test_organization_delete_clears_members_and_invites(sync_db):
    """The registry half, where it actually runs (the endpoint does its destructive work through
    the sync session, so this is tested directly rather than over HTTP)."""
    from tracely.infrastructure.db import repositories

    with sync_db() as s:
        s.add(models.Organization(id="o1", name="Acme", slug="acme", kind="company"))
        s.add(models.OrgMembership(id="m1", organization_id="o1", user_id="u1", role="OWNER"))
        s.add(models.OrgMembership(id="m2", organization_id="o1", user_id="u2", role="MEMBER"))
        s.add(models.OrgMembership(id="m3", organization_id="o2", user_id="u1", role="OWNER"))
        s.commit()

        counts = repositories.organization_delete(s, "o1")
        assert counts["organization_memberships"] == 2
        assert counts["organizations"] == 1
        assert s.get(models.Organization, "o1") is None
        assert s.get(models.OrgMembership, "m3") is not None  # another org is untouched


@pytest.fixture
def sync_registry(sync_db, monkeypatch):
    """The delete endpoint does its destructive half through `SyncSessionLocal`, which no
    dependency override reaches — unpatched the test talks to a real Postgres (green on a dev
    box, connection-refused in CI). It's empty, so the endpoint deletes nothing; the registry
    half is covered by test_organization_delete_clears_members_and_invites."""
    from tracely.api.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "SyncSessionLocal", sync_db)


async def test_owner_can_delete_their_organization(client, hosted, sync_registry):
    """The HTTP contract: the guards pass and the caller is handed somewhere to land."""
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    created = await client.post(
        "/auth/organizations", json={"name": "Acme"}, headers=_bearer(token)
    )
    org_id = created.json()["id"]
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    inside = next(p["id"] for p in me["projects"] if p["organization_id"] == org_id)
    h = {**_bearer(token), "X-Tracely-Project": inside}

    r = await client.request(
        "DELETE", "/auth/organizations", json={"confirm": "Acme"}, headers=h
    )
    assert r.status_code == 200, r.text
    # A workspace outside the deleted org — the cookie is repointed at it, so the caller isn't
    # left holding a dead id (which would 403 every request and bounce them to /login).
    survivor = r.json()["switch_to"]
    assert survivor and survivor != inside
    assert survivor in {p["id"] for p in me["projects"] if p["organization_id"] != org_id}


async def test_delete_requires_the_exact_name(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    await client.post("/auth/organizations", json={"name": "Acme"}, headers=_bearer(token))
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    org = next(o for o in me["organizations"] if o["kind"] == "company")
    inside = next(p["id"] for p in me["projects"] if p["organization_id"] == org["id"])
    h = {**_bearer(token), "X-Tracely-Project": inside}

    r = await client.request(
        "DELETE", "/auth/organizations", json={"confirm": "DELETE"}, headers=h
    )
    assert r.status_code == 400  # a fixed word must not work — it's the name or nothing


async def test_a_personal_account_cannot_be_deleted(client, hosted):
    await _signup(client, "founder@x.test")
    token = await _signup(client, "solo@x.test")
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    r = await client.request(
        "DELETE", "/auth/organizations",
        json={"confirm": me["organization_name"]}, headers=_bearer(token),
    )
    assert r.status_code == 400 and "personal" in r.json()["detail"]


async def test_cannot_delete_your_only_organization(client, hosted):
    """The self-host founder has one company org and no personal fallback — deleting it would
    leave them with nothing to sign in to."""
    token = await _signup(client, "founder@x.test")
    me = (await client.get("/auth/me", headers=_bearer(token))).json()
    r = await client.request(
        "DELETE", "/auth/organizations",
        json={"confirm": me["organization_name"]}, headers=_bearer(token),
    )
    assert r.status_code == 409 and "only organization" in r.json()["detail"]


async def test_members_cannot_delete_the_organization(client, hosted):
    owner = await _company_token(client)
    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test", "role": "ADMIN"},
        headers=_bearer(owner),
    )
    joined = await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    me = (await client.get("/auth/me", headers=_bearer(owner))).json()
    r = await client.request(
        "DELETE", "/auth/organizations",
        json={"confirm": me["organization_name"]},
        headers=_bearer(joined.json()["token"]),
    )
    assert r.status_code == 403  # ADMIN is not enough for this one


async def test_a_plain_member_cannot_invite_or_add_workspaces(client, hosted):
    token = await _company_token(client)
    inv = await client.post(
        "/auth/invitations", json={"email": "teammate@x.test"}, headers=_bearer(token)
    )
    member = await client.post(
        "/auth/invitations/accept",
        json={"token": inv.json()["token"], "password": "member-pw-1"},
    )
    mh = _bearer(member.json()["token"])
    assert (
        await client.post("/auth/invitations", json={"email": "x@x.test"}, headers=mh)
    ).status_code == 403
    assert (
        await client.post("/auth/projects", json={"name": "Sneaky"}, headers=mh)
    ).status_code == 403


# ── leaving, and being removed ────────────────────────────────────────────────


async def _invite_and_accept(client, owner_token, email, role="MEMBER", password="member-pw-1"):
    """Put `email` in the owner's org and hand back their session token + user id."""
    inv = await client.post(
        "/auth/invitations", json={"email": email, "role": role}, headers=_bearer(owner_token)
    )
    assert inv.status_code == 200, inv.text
    joined = await client.post(
        "/auth/invitations/accept", json={"token": inv.json()["token"], "password": password}
    )
    assert joined.status_code == 200, joined.text
    return joined.json()["token"], joined.json()["user_id"]


async def test_removing_a_member_revokes_every_workspace_in_the_org(client, hosted):
    """The point of the endpoint: access is derived from the seat, so dropping it locks the
    door — no per-workspace cleanup to forget."""
    owner = await _company_token(client)
    await client.post("/auth/projects", json={"name": "W2"}, headers=_bearer(owner))
    mtoken, muid = await _invite_and_accept(client, owner, "teammate@x.test")
    projects = [p["id"] for p in (await client.get("/auth/me", headers=_bearer(mtoken))).json()["projects"]]
    assert len(projects) == 2

    r = await client.delete(f"/auth/members/{muid}", headers=_bearer(owner))
    assert r.status_code == 200, r.text

    for pid in projects:
        probe = await client.get("/auth/me", headers={**_bearer(mtoken), "X-Tracely-Project": pid})
        assert probe.status_code == 403
    assert (await client.get("/auth/me", headers=_bearer(mtoken))).status_code == 403
    rows = (await client.get("/auth/members", headers=_bearer(owner))).json()
    assert [r["email"] for r in rows] == ["boss@x.test"]


async def test_leaving_lands_you_in_your_remaining_workspace(client, hosted):
    """Someone who already had their own account keeps it; `switch_to` is where the UI puts them
    so the next request isn't aimed at a workspace they just lost."""
    await _signup(client, "boss@x.test")  # first registrant claims the company org
    own = await _signup(client, "nomad@x.test")  # ...so this one gets a personal org
    personal = (await client.get("/auth/me", headers=_bearer(own))).json()
    boss = await client.post(
        "/auth/login", json={"email": "boss@x.test", "password": "hunter2-pw"}
    )
    btoken = boss.json()["token"]
    company = (await client.get("/auth/me", headers=_bearer(btoken))).json()
    # nomad already has an account, so the invite is accepted with THEIR password — an invite
    # token alone can't log anyone into an existing account (see test_invite_takeover.py).
    mtoken, muid = await _invite_and_accept(client, btoken, "nomad@x.test", password="hunter2-pw")

    # You leave the org backing your ACTIVE workspace — the header is what the frontend's
    # active-workspace cookie forwards, and without it "which org?" is whichever is oldest.
    r = await client.delete(
        f"/auth/members/{muid}",
        headers={**_bearer(mtoken), "X-Tracely-Project": company["project_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["switch_to"] == personal["project_id"]
    after = (await client.get("/auth/me", headers=_bearer(mtoken))).json()
    assert [o["id"] for o in after["organizations"]] == [personal["organization_id"]]


async def test_leaving_your_only_organization_lands_you_in_a_personal_one(client, hosted):
    """An account created BY INVITE has no personal org, so "leave" used to 409 — which meant the
    people most likely to want out (invited teammates) were the ones who couldn't get out. They
    get a personal org on the way through instead."""
    owner = await _company_token(client)
    mtoken, muid = await _invite_and_accept(client, owner, "teammate@x.test")
    before = (await client.get("/auth/me", headers=_bearer(mtoken))).json()

    r = await client.delete(f"/auth/members/{muid}", headers=_bearer(mtoken))
    assert r.status_code == 200, r.text

    after = (await client.get("/auth/me", headers=_bearer(mtoken))).json()
    orgs = after["organizations"]
    assert len(orgs) == 1 and orgs[0]["id"] != before["organization_id"]
    # and the handed-back workspace is one they can actually reach
    assert r.json()["switch_to"] == after["project_id"]

    # the company they left no longer counts them as a seat
    members = (await client.get("/auth/members", headers=_bearer(owner))).json()
    assert muid not in [m["user_id"] for m in members]


async def test_the_last_owner_cannot_leave_or_be_removed(client, hosted):
    """Otherwise the org survives with nobody able to invite, bill or delete it."""
    owner = await _company_token(client)
    me = (await client.get("/auth/me", headers=_bearer(owner))).json()
    admin_token, _ = await _invite_and_accept(client, owner, "admin@x.test", role="ADMIN")

    r = await client.delete(f"/auth/members/{me['user_id']}", headers=_bearer(owner))
    assert r.status_code == 409 and "must keep an owner" in r.json()["detail"]
    # and an admin can't take the owner's seat either
    r = await client.delete(f"/auth/members/{me['user_id']}", headers=_bearer(admin_token))
    assert r.status_code == 403


async def test_a_plain_member_cannot_remove_a_teammate(client, hosted, monkeypatch):
    monkeypatch.setattr(settings, "free_seat_limit", 4)  # owner + two teammates
    owner = await _company_token(client)
    mtoken, _ = await _invite_and_accept(client, owner, "one@x.test")
    _other, other_uid = await _invite_and_accept(
        client, owner, "two@x.test", password="member-pw-2"
    )
    r = await client.delete(f"/auth/members/{other_uid}", headers=_bearer(mtoken))
    assert r.status_code == 403


async def test_a_personal_account_cannot_be_left(client, hosted):
    """It is the user's own login, not a team they joined."""
    await _signup(client, "boss@x.test")
    solo = await _signup(client, "solo@x.test")
    me = (await client.get("/auth/me", headers=_bearer(solo))).json()
    r = await client.delete(f"/auth/members/{me['user_id']}", headers=_bearer(solo))
    assert r.status_code == 400


async def test_removal_cannot_be_aimed_outside_the_callers_org(client, hosted):
    """No org id in the path, so the worst a hostile owner can do is 404 on a stranger."""
    boss = await _company_token(client)
    outsider = await _signup(client, "outsider@x.test")
    ouid = (await client.get("/auth/me", headers=_bearer(outsider))).json()["user_id"]
    r = await client.delete(f"/auth/members/{ouid}", headers=_bearer(boss))
    assert r.status_code == 404
    assert (await client.get("/auth/me", headers=_bearer(outsider))).status_code == 200
