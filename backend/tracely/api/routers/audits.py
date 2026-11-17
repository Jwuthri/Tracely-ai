"""Audits: definitions, seeds, runs and rollouts (`domain/audit`, `services/audit_service.py`).

Pure HTTP shaping — Postgres through `infrastructure.db.repositories`, the run logic through
`AuditService`.

Who may do what follows the ingest-key rule ("a leaked CI key must cost traces, not the
workspace"): writing or deleting an audit or its seeds is `require_user`, because an audit's
config is where its budget ceiling lives. STARTING a run stays open to an ingest key — CI will
need it — but a run can never be given more than the audit's configured budget.
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_user
from tracely.config import settings
from tracely.domain.audit import dimensions as audit_catalog
from tracely.domain.audit import media
from tracely.domain.audit.config import normalize, resolved_dimensions
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Audit, AuditRollout, AuditRun, AuditSeed
from tracely.services.audit_service import AuditError, AuditService

router = APIRouter(prefix="/api")

_MAX_SEEDS = 500
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
        "attachments": seed.attachments or [],
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
        "stage": int(r.stage or 0),
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
    """Built-in dimensions and behaviour presets for the create form, and whether this deployment
    can give a target a real machine (the form only offers what can actually run)."""
    return {
        **audit_catalog.catalog(),
        "sandbox": {
            "available": bool(settings.sandbox_backend),
            "network": bool(settings.sandbox_backend and settings.sandbox_egress_network),
        },
    }


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


def _seed_call(fn):
    """Run a seed-producing service call and shape its failures."""
    try:
        return [_seed_dict(x) for x in fn()]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except AuditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:  # a model that would not produce the schema, a provider error
        raise HTTPException(status_code=502, detail=f"seed generation failed: {str(exc)[:300]}") from None


@router.post("/audits/{audit_id}/seeds/generate", dependencies=[Depends(require_user)])
async def generate_seeds(
    audit_id: str, project_id: str = Depends(get_project_id), body: dict = Body(default={})
):
    """Write a batch of new seeds from the audit's behaviour (up to 8 per call). Call again for
    more: each call is told what already exists."""
    def work():
        with SyncSessionLocal() as s:
            return _seed_call(lambda: AuditService(s).generate_seeds(
                project_id, audit_id, n=int(body.get("n") or 8), guidance=str(body.get("guidance") or "")))

    return await run_in_threadpool(work)


@router.post("/audits/{audit_id}/seeds/import", dependencies=[Depends(require_user)])
async def import_seeds(
    audit_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    """Copy seeds from the Petri library, by `tag` or by `ids`."""
    tag, ids = str(body.get("tag") or ""), body.get("ids")
    if not tag and not ids:
        raise HTTPException(status_code=400, detail="tag or ids required")

    def work():
        with SyncSessionLocal() as s:
            return _seed_call(lambda: AuditService(s).import_library_seeds(
                project_id, audit_id, tag=tag, ids=[str(i) for i in ids] if ids else None))

    return await run_in_threadpool(work)


@router.post("/audits/{audit_id}/seeds/{seed_id}/attachments", dependencies=[Depends(require_user)])
async def attach_to_seed(
    audit_id: str, seed_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    """Attach an image or audio file to a seed: `{name, data (base64), text?}`. The attacker can
    send it to the target; `text` says what it shows or says, for judges that cannot see or hear."""
    try:
        data = base64.b64decode(str(body.get("data") or ""), validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="data must be base64") from None

    def work():
        with SyncSessionLocal() as s:
            try:
                return _seed_dict(AuditService(s).attach_to_seed(
                    project_id, audit_id, seed_id, data, str(body.get("name") or "file"), str(body.get("text") or "")))
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
            except AuditError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None

    return await run_in_threadpool(work)


@router.delete("/audits/{audit_id}/seeds/{seed_id}/attachments/{asset}", dependencies=[Depends(require_user)])
async def detach_from_seed(audit_id: str, seed_id: str, asset: str, project_id: str = Depends(get_project_id)):
    def work():
        with SyncSessionLocal() as s:
            seed = s.get(AuditSeed, seed_id)
            if seed is None or seed.audit_id != audit_id or seed.project_id != project_id:
                raise HTTPException(status_code=404, detail="seed not found")
            seed.attachments = [a for a in seed.attachments or [] if a.get("asset") != asset]
            s.commit()
            return _seed_dict(seed)

    return await run_in_threadpool(work)


@router.get("/audit-assets/{asset}")
async def audit_asset(asset: str, project_id: str = Depends(get_project_id)):
    """An audit's image or audio file, by content hash. The key is under the caller's project
    prefix, so a hash from another workspace simply is not there."""
    if not _SHA256.fullmatch(asset):
        raise HTTPException(status_code=404, detail="not found")

    def work():
        try:
            return blobstore.get_blob_typed(blobstore.audit_blob_key(project_id, f"assets/{asset}"))
        except Exception:
            raise HTTPException(status_code=404, detail="not found") from None

    data, mime = await run_in_threadpool(work)
    # Sniffed again on the way out: the stored type came from the same sniff, and a file that is
    # not media is never served as something a browser would run.
    if not media.sniff(data):
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
    })


@router.post("/audits/{audit_id}/seeds/{seed_id}/variations", dependencies=[Depends(require_user)])
async def seed_variations(
    audit_id: str, seed_id: str, project_id: str = Depends(get_project_id), body: dict = Body(...)
):
    """Write one variation of a seed per axis in `dimensions`."""
    def work():
        with SyncSessionLocal() as s:
            return _seed_call(lambda: AuditService(s).generate_variations(
                project_id, audit_id, seed_id, [str(d) for d in body.get("dimensions") or []]))

    return await run_in_threadpool(work)


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
