"""Project-level settings: destructive maintenance (wipe the workspace's data) + the workspace's
own OpenRouter key for LLM eval spend.

Pure HTTP shaping — ClickHouse deletes live in `infrastructure.clickhouse.deletes`, Postgres in
`infrastructure.db.repositories`, encryption in `infrastructure.llm.provider`.
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_role, require_user
from tracely.auth import Principal
from tracely.infrastructure.blob import s3
from tracely.infrastructure.clickhouse import async_reader, deletes
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.llm import checkpointer, provider
from tracely.services import demo_seed

log = structlog.get_logger()

router = APIRouter(prefix="/api")

CONFIRM = "DELETE"


class WipeBody(BaseModel):
    confirm: str = ""


@router.delete("/project/data", dependencies=[Depends(require_user)])
async def wipe_project_data(body: WipeBody, project_id: str = Depends(get_project_id)) -> dict:
    """Delete every trace, score and everything derived from them in this project.

    Keeps the project itself, ingest keys, users, evaluators and monitors — your configuration, so
    the workspace is immediately usable again. Send `{"confirm": "DELETE"}`; anything else is a
    400, which is the whole guard against a stray curl.

    Not transactional across the two stores: ClickHouse goes first, then Postgres. If the Postgres
    half fails you're left with derived rows pointing at deleted traces — run it again, it's
    idempotent.
    """
    if body.confirm != CONFIRM:
        raise HTTPException(status_code=400, detail=f"confirm must be exactly '{CONFIRM}'")

    events = await deletes.delete_project_events(project_id)

    def work():
        with SyncSessionLocal() as s:
            return repo.project_data_delete(s, project_id)

    registry = await run_in_threadpool(work)
    # The judge's own conversations: LangGraph's tables, so not reachable from the registry
    # session above. They quote the customer's messages verbatim, and `project_data_delete` has
    # just removed the chain-progress rows they pair with — leaving them would keep a copy of the
    # very data this endpoint promises to delete.
    chats = await run_in_threadpool(checkpointer.delete_project_chats, project_id)
    return {"deleted": {**events, **registry, **({"judge_chats": chats} if chats else {})}}


@router.post("/project/agents/prune", dependencies=[Depends(require_user)])
async def prune_unused_agents(project_id: str = Depends(get_project_id)) -> dict:
    """Delete registered agents that have no spans and nothing referencing them.

    Agents are derived from what traces DECLARE (`tracely.agent.id`), so this clears out the ones
    an earlier attribution rule invented from framework attributes — every sub-agent an OpenAI
    Agents / CrewAI / ADK harness spins up used to land in the registry. Safe and idempotent: an
    agent still referenced by a scenario, case, gate or endpoint is kept.
    """
    live = await async_reader.agent_ids_with_spans(project_id)

    def work() -> list[str]:
        with SyncSessionLocal() as s:
            return repo.agents_prune(s, project_id, live)

    pruned = await run_in_threadpool(work)
    log.info("agents_pruned", project_id=project_id, count=len(pruned))
    return {"pruned": pruned, "kept": len(live)}


@router.delete("/project")
async def delete_workspace(
    body: WipeBody, principal: Principal = Depends(require_role("OWNER", "ADMIN"))
) -> dict:
    """Delete this workspace and everything in it — traces, blobs, config, the workspace itself.

    Owners and admins only, and only the workspace you're currently in (there is no id parameter,
    so no way to aim this at someone else's). Confirm by sending the workspace's exact name.

    Refuses to delete an organization's LAST workspace: access is derived from the org, so an org
    with no workspaces locks every one of its members out of the product with a 403 and no way
    back through the UI. The surviving sibling also inherits this workspace's usage counters, so
    deleting a workspace can't be used to reset the month's quota.
    """
    project_id = principal.project_id

    def load():
        with SyncSessionLocal() as s:
            project = repo.project_get(s, project_id)
            return (project.name if project else None), repo.project_siblings(s, project_id)

    name, siblings = await run_in_threadpool(load)
    if name is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not siblings:
        raise HTTPException(
            status_code=409,
            detail=(
                "this is your organization's only workspace — everyone reaches Tracely through "
                "it. Create another workspace first, or delete the organization itself."
            ),
        )
    if body.confirm != name:
        raise HTTPException(
            status_code=400, detail=f"confirm must be exactly the workspace name ('{name}')"
        )

    await deletes.delete_project_events(project_id)

    def work():
        with SyncSessionLocal() as s:
            return repo.project_delete(s, project_id, usage_heir_id=siblings[0])

    deleted = await run_in_threadpool(work)
    deleted["blobs"] = await run_in_threadpool(s3.delete_project_blobs, project_id)
    # A deleted workspace never grades again, so nothing would ever reset these conversations —
    # without this they would sit until the 90-day retention sweep reached them.
    if chats := await run_in_threadpool(checkpointer.delete_project_chats, project_id):
        deleted["judge_chats"] = chats
    log.info("workspace_deleted", project_id=project_id, by=principal.user_id)
    # The caller's active-workspace cookie now points at a dead id; tell it where to go instead.
    return {"deleted": deleted, "switch_to": siblings[0]}


@router.post("/project/seed", dependencies=[Depends(require_user)])
async def seed_project_demo(project_id: str = Depends(get_project_id)) -> dict:
    """Populate this workspace with the demo dataset — traces, clusters, cases, gates, scenarios.

    Available in every environment, prod included: it's how a workspace stops being a set of empty
    pages. Queued, not inline — the seeder drives the product through its own HTTP API and takes
    minutes. Idempotent, so a double-click just costs time.
    """

    def key():
        with SyncSessionLocal() as s:
            return repo.project_ingest_key(s, project_id)

    ingest_key = await run_in_threadpool(key)
    if not ingest_key:
        raise HTTPException(status_code=400, detail="this workspace has no ingest key")
    if not demo_seed.available():  # trimmed image that ships without scripts/
        raise HTTPException(status_code=501, detail="seed_demo.py is not present in this image")
    demo_seed.launch(ingest_key)
    return {"queued": True}


# ── workspace OpenRouter key ──────────────────────────────────────────────────


class OpenRouterKeyIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=200)


class OpenRouterKeyOut(BaseModel):
    configured: bool


class UiPrefsBody(BaseModel):
    # free-form but tiny: today only `hiddenTypes: list[str]`. Size-capped so this can never
    # become an unbounded dumping ground.
    prefs: dict

    @classmethod
    def validate_size(cls, prefs: dict) -> dict:
        if len(json.dumps(prefs)) > 4096:
            raise HTTPException(status_code=400, detail="prefs too large")
        return prefs


@router.get("/project/ui-prefs")
async def get_ui_prefs(project_id: str = Depends(get_project_id)) -> dict:
    """Workspace UI defaults (e.g. `hiddenTypes`). A browser's explicit local filter wins over
    these; absent one, every member's views start from here."""

    def work() -> dict:
        with SyncSessionLocal() as s:
            return repo.project_ui_prefs_get(s, project_id)

    return {"prefs": await run_in_threadpool(work)}


@router.put("/project/ui-prefs")
async def set_ui_prefs(body: UiPrefsBody, project_id: str = Depends(get_project_id)) -> dict:
    """Replace the workspace UI defaults. Deliberately NOT behind `require_user`: these are
    cosmetic view defaults — no data, no secrets, nothing destroyed — and dev mode has no
    human principal to satisfy."""
    prefs = UiPrefsBody.validate_size(body.prefs)

    def work() -> dict:
        with SyncSessionLocal() as s:
            return repo.project_ui_prefs_set(s, project_id, prefs)

    return {"prefs": await run_in_threadpool(work)}


@router.get("/project/llm-key", response_model=OpenRouterKeyOut)
async def get_llm_key(project_id: str = Depends(get_project_id)) -> OpenRouterKeyOut:
    def work() -> bool:
        with SyncSessionLocal() as s:
            proj = repo.project_get(s, project_id)
            return bool(proj and proj.openrouter_api_key_encrypted)

    return OpenRouterKeyOut(configured=await run_in_threadpool(work))


@router.put("/project/llm-key", response_model=OpenRouterKeyOut, dependencies=[Depends(require_user)])
async def set_llm_key(
    body: OpenRouterKeyIn, project_id: str = Depends(get_project_id)
) -> OpenRouterKeyOut:
    """Set this workspace's own OpenRouter key. It is REQUIRED for every LLM-backed feature
    (judge evaluators, clustering/failure intelligence, meta-analysis, rolling summary, scenario
    gates) — without one those degrade to no-op. The key itself is never returned again; only
    whether one is configured."""
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key is required")
    try:
        encrypted = provider.encrypt_project_key(key)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None

    def work() -> bool:
        with SyncSessionLocal() as s:
            return repo.project_set_openrouter_key(s, project_id, encrypted)

    if not await run_in_threadpool(work):
        raise HTTPException(status_code=404, detail="project not found")
    provider.invalidate_project_key(project_id)
    return OpenRouterKeyOut(configured=True)


@router.delete("/project/llm-key", response_model=OpenRouterKeyOut, dependencies=[Depends(require_user)])
async def clear_llm_key(project_id: str = Depends(get_project_id)) -> OpenRouterKeyOut:
    """Clear the workspace key — every LLM-backed feature stops running for this project."""

    def work() -> bool:
        with SyncSessionLocal() as s:
            return repo.project_set_openrouter_key(s, project_id, None)

    await run_in_threadpool(work)
    provider.invalidate_project_key(project_id)
    return OpenRouterKeyOut(configured=False)
