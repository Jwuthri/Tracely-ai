"""Audits: definitions, seeds, runs and rollouts (`domain/audit`, `services/audit_service.py`).

Pure HTTP shaping — Postgres through `infrastructure.db.repositories`, the run logic through
`AuditService`.

Who may do what follows the ingest-key rule ("a leaked CI key must cost traces, not the
workspace"): writing or deleting an audit or its seeds is `require_user`, because an audit's
config is where its budget ceiling lives. STARTING a run stays open to an ingest key — CI will
need it — but a run can never be given more than the audit's configured budget.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_user
from tracely.config import settings
from tracely.domain.audit import dimensions as audit_catalog
from tracely.domain.audit.config import normalize, resolved_dimensions
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Audit, AuditRollout, AuditRun, AuditSeed
from tracely.services.audit_service import AuditError, AuditService

router = APIRouter(prefix="/api")

_MAX_SEEDS = 500


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _run_dict(run: AuditRun) -> dict[str, Any]:
    return {
        "id": run.id, "audit_id": run.audit_id, "status": run.status, "mode": run.mode,
        "budget_usd": run.budget_usd, "spend_usd": round(float(run.spend_usd or 0), 4),
        "total": run.total, "done": run.done, "summary": run.summary or {},
        "created_at": _iso(run.created_at), "finished_at": _iso(run.finished_at),
    }


def _seed_dict(seed: AuditSeed) -> dict[str, Any]:
    return {
        "id": seed.id, "title": seed.title, "instruction": seed.instruction,
        "tags": seed.tags or [], "origin": seed.origin, "parent_seed_id": seed.parent_seed_id,
        "enabled": seed.enabled, "created_at": _iso(seed.created_at),
    }


def _audit_dict(audit: Audit) -> dict[str, Any]:
    config = audit.config or {}
    return {
        "id": audit.id, "name": audit.name, "agent": audit.agent_slug, "behavior": audit.behavior,
        "config": config,
        # What a run will actually judge: the audit's dimensions plus the validity ones.
        "dimensions": resolved_dimensions(config),
        "created_at": _iso(audit.created_at),
    }


def _rollout_dict(r: AuditRollout) -> dict[str, Any]:
    return {
        "id": r.id, "seed_id": r.seed_id, "title": (r.seed or {}).get("title") or "",
        "rep": r.rep, "position": r.position, "phase": r.phase, "verdict": r.verdict,
        "results": (r.scores or {}).get("results") or [],
        "detail": r.detail or {}, "spend_usd": round(float(r.spend_usd or 0), 4),
        "conversation_id": f"audit:{r.id}", "finished_at": _iso(r.finished_at),
    }


def _config_or_400(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="config must be an object")
    try:
        return normalize(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _seed_fields(body: dict) -> tuple[str, str]:
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="a seed needs an instruction")
    return str(body.get("title") or "").strip()[:512], instruction[:20000]


# ── catalog + target import (declared before `/audits/{audit_id}`) ────────────


@router.get("/audits/catalog")
async def audits_catalog(project_id: str = Depends(get_project_id)):
    """Built-in dimensions and behaviour presets for the create form."""
    return audit_catalog.catalog()


@router.post("/audits/import-target")
async def import_target(project_id: str = Depends(get_project_id), body: dict = Body(...)):
    """Rebuild a target from one of this workspace's agents: the system prompt, tools and model of
    its most recent real trace."""
    agent = str(body.get("agent") or "").strip()
    if not agent:
        raise HTTPException(status_code=400, detail="agent required")

    def work():
        with SyncSessionLocal() as s:
            return AuditService(s).import_target(project_id, agent)

    try:
        return await run_in_threadpool(work)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


# ── audits ────────────────────────────────────────────────────────────────────


@router.get("/audits")
async def list_audits(project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            seeds = repo.audit_seed_counts(s, project_id)
            latest = repo.audit_latest_runs(s, project_id)
            return [
                {
                    **_audit_dict(a), "seeds": seeds.get(a.id, 0),
                    "last_run": _run_dict(latest[a.id]) if a.id in latest else None,
                }
                for a in repo.audits_list(s, project_id)
            ]

    return await run_in_threadpool(work)


@router.post("/audits", dependencies=[Depends(require_user)])
async def create_audit(project_id: str = Depends(get_project_id), body: dict = Body(...)):
    """Create an audit, optionally with its first seeds (`seeds: [{title, instruction}]`)."""
    name = str(body.get("name") or "").strip()[:200]
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    config = _config_or_400(body.get("config"))
    seeds = [_seed_fields(x) for x in (body.get("seeds") or []) if isinstance(x, dict)][:_MAX_SEEDS]
    origin = "preset" if body.get("preset") else "manual"

    def work():
        with SyncSessionLocal() as s:
            audit = Audit(
                id=str(uuid.uuid4()), project_id=project_id, name=name,
                agent_slug=str(body.get("agent") or "").strip()[:128],
                behavior=str(body.get("behavior") or "").strip()[:8000], config=config,
            )
            s.add(audit)
            s.flush()  # the audit row must exist before the seeds that reference it
            for title, instruction in seeds:
                s.add(AuditSeed(
                    id=str(uuid.uuid4()), audit_id=audit.id, project_id=project_id,
                    title=title, instruction=instruction, origin=origin,
                ))
            s.commit()
            return {**_audit_dict(audit), "seeds": len(seeds)}

    return await run_in_threadpool(work)


@router.get("/audits/{audit_id}")
async def get_audit(audit_id: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            audit = repo.audit_get(s, project_id, audit_id)
            if audit is None:
                return None
            return {
                **_audit_dict(audit),
                "seeds": [_seed_dict(x) for x in repo.audit_seeds_list(s, audit_id)],
                "runs": [_run_dict(r) for r in repo.audit_runs_list(s, audit_id)],
            }

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return out


@router.patch("/audits/{audit_id}", dependencies=[Depends(require_user)])
async def update_audit(
    audit_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    """Partial: only the keys present are touched. A new `config` replaces the old one whole —
    past runs are unaffected, each carries its own snapshot."""
    config = _config_or_400(body["config"]) if "config" in body else None

    def work():
        with SyncSessionLocal() as s:
            audit = repo.audit_get(s, project_id, audit_id)
            if audit is None:
                return None
            if "name" in body and str(body["name"]).strip():
                audit.name = str(body["name"]).strip()[:200]
            if "behavior" in body:
                audit.behavior = str(body["behavior"] or "").strip()[:8000]
            if "agent" in body:
                audit.agent_slug = str(body["agent"] or "").strip()[:128]
            if config is not None:
                audit.config = config
            s.commit()
            return _audit_dict(audit)

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return out


@router.delete("/audits/{audit_id}", dependencies=[Depends(require_user)])
async def delete_audit(audit_id: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            return repo.audit_delete(s, project_id, audit_id)

    if not await run_in_threadpool(work):
        raise HTTPException(status_code=404, detail="audit not found")
    return {"deleted": audit_id}


# ── seeds ─────────────────────────────────────────────────────────────────────


@router.post("/audits/{audit_id}/seeds", dependencies=[Depends(require_user)])
async def add_seed(
    audit_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    title, instruction = _seed_fields(body)

    def work():
        with SyncSessionLocal() as s:
            if repo.audit_get(s, project_id, audit_id) is None:
                return None
            seed = AuditSeed(
                id=str(uuid.uuid4()), audit_id=audit_id, project_id=project_id,
                title=title, instruction=instruction,
                tags=[str(t)[:48] for t in (body.get("tags") or []) if t][:12],
            )
            s.add(seed)
            s.commit()
            return _seed_dict(seed)

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return out


@router.patch("/audits/{audit_id}/seeds/{seed_id}", dependencies=[Depends(require_user)])
async def update_seed(
    audit_id: str, seed_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    def work():
        with SyncSessionLocal() as s:
            seed = s.get(AuditSeed, seed_id)
            if seed is None or seed.project_id != project_id or seed.audit_id != audit_id:
                return None
            if "title" in body:
                seed.title = str(body["title"] or "").strip()[:512]
            if "instruction" in body and str(body["instruction"] or "").strip():
                seed.instruction = str(body["instruction"]).strip()[:20000]
            if "enabled" in body:
                seed.enabled = bool(body["enabled"])
            s.commit()
            return _seed_dict(seed)

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="seed not found")
    return out


@router.delete("/audits/{audit_id}/seeds/{seed_id}", dependencies=[Depends(require_user)])
async def delete_seed(audit_id: str, seed_id: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            seed = s.get(AuditSeed, seed_id)
            if seed is None or seed.project_id != project_id or seed.audit_id != audit_id:
                return False
            # Past rollouts carry their own copy of the seed (`audit_rollouts.seed`), so their
            # results stay readable after it is gone.
            s.delete(seed)
            s.commit()
            return True

    if not await run_in_threadpool(work):
        raise HTTPException(status_code=404, detail="seed not found")
    return {"deleted": seed_id}


# ── runs ──────────────────────────────────────────────────────────────────────


def _enqueue(run_id: str, rollouts: int) -> None:
    from tracely.workers.tasks import audit_tick_task

    # One tick chain per parallel rollout. Extra chains would be harmless — the cap is enforced
    # where a rollout is claimed — but they would be wasted wake-ups.
    for _ in range(max(1, min(settings.audit_max_parallel, rollouts))):
        audit_tick_task.apply_async((run_id,))


@router.post("/audits/{audit_id}/runs")
async def start_run(
    audit_id: str, project_id: str = Depends(get_project_id), body: dict = Body(default={})
):
    """Start a run. Returns at once with the run id; the worker advances it in slices."""
    seed_ids = body.get("seed_ids")
    if seed_ids is not None and not isinstance(seed_ids, list):
        raise HTTPException(status_code=400, detail="seed_ids must be a list")

    def work():
        with SyncSessionLocal() as s:
            run = AuditService(s).start_run(
                project_id, audit_id,
                seed_ids=[str(x) for x in seed_ids] if seed_ids else None,
                reps=int(body["reps"]) if body.get("reps") else None,
                budget_usd=float(body["budget_usd"]) if body.get("budget_usd") else None,
                mode=str(body.get("mode") or "measure"),
            )
            return _run_dict(run)

    try:
        run = await run_in_threadpool(work)
    except LookupError:
        raise HTTPException(status_code=404, detail="audit not found") from None
    except (AuditError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _enqueue(run["id"], run["total"])
    return run


@router.get("/audit-runs/{run_id}")
async def get_run(run_id: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            run = repo.audit_run_get(s, project_id, run_id)
            if run is None:
                return None
            audit = repo.audit_get(s, project_id, run.audit_id)
            return {
                **_run_dict(run),
                "audit": {"id": run.audit_id, "name": audit.name if audit else ""},
                "config": run.config or {},
                "dimensions": resolved_dimensions(run.config or {}),
                "rollouts": [_rollout_dict(r) for r in repo.audit_rollouts_list(s, run_id)],
            }

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="run not found")
    return out


@router.post("/audit-runs/{run_id}/cancel", dependencies=[Depends(require_user)])
async def cancel_run(run_id: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            run = AuditService(s).cancel_run(project_id, run_id)
            return _run_dict(run) if run else None

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="run not found")
    return out


@router.post("/audit-runs/{run_id}/resume")
async def resume_run(run_id: str, project_id: str = Depends(get_project_id)):
    """Nudge a RUNNING run whose worker went away. The beat watchdog does this on its own; this
    is for deployments with no beat process."""
    def work():
        with SyncSessionLocal() as s:
            run = repo.audit_run_get(s, project_id, run_id)
            return _run_dict(run) if run else None

    run = await run_in_threadpool(work)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["status"] == "RUNNING":
        _enqueue(run_id, 1)
    return run


@router.get("/audit-rollouts/{rollout_id}")
async def get_rollout(rollout_id: str, project_id: str = Depends(get_project_id)):
    """One rollout with its full transcript: every branch, the attacker's own side, and each
    judge's answer. Read from the checkpoint, not from spans — spans hold only the final branch."""
    def work():
        with SyncSessionLocal() as s:
            r = repo.audit_rollout_get(s, project_id, rollout_id)
            if r is None:
                return None
            state = AuditService(s).rollout_state(project_id, rollout_id) or {}
            return {
                **_rollout_dict(r), "run_id": r.run_id, "seed": r.seed or {},
                "cells": (r.scores or {}).get("cells") or [],
                "transcript": {
                    "system_prompt": state.get("system_prompt") or "",
                    "control": state.get("control") or "",
                    "tools": [
                        {"name": t.get("name"), "description": t.get("description"),
                         "environment": t.get("environment") or ""}
                        for t in state.get("tools") or []
                    ],
                    "nodes": [state["nodes"][i] for i in state.get("order") or [] if i in state.get("nodes", {})],
                    "branches": state.get("branches") or [],
                    "head": state.get("head") or "",
                    # The attacker's system prompt is the same for every rollout of a seed and
                    # long; its plan, tool calls and what it was told back are what matter.
                    "attacker": (state.get("auditor_messages") or [])[1:],
                },
            }

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="rollout not found")
    return out
