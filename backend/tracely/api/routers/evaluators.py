"""Evaluator (= evaluation column) management: CRUD + the template library + AI generation.

An evaluator row IS a column in the trace table: `score_name` is the stable key results are
stored under, `level` picks the row granularity (CONVERSATION / AGENT_RUN / SPAN…), and
`config` carries the kind-specific knobs (judge prompt, threshold, output_type, model;
structural check + params). Deleting an evaluator keeps its historical scores (they're keyed
by name in ClickHouse) — the column simply disappears from the grid.

Pure HTTP shaping — all Postgres access lives in `infrastructure.db.repositories`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_user
from tracely.domain.evaluation import decision
from tracely.domain.evaluation.evaluators import TEMPLATES
from tracely.domain.evaluation.evaluators.llm_judge import OUTPUT_TYPES
from tracely.domain.evaluation.generation import generate_evaluator_config
from tracely.domain.evaluation.output_schema import model_from_json_schema
from tracely.domain.evaluation.template_resolver import (
    build_context,
    extract_template_variables,
    template_resolver,
    variables_for_level_json,
)
from tracely.infrastructure.clickhouse import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Evaluator
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.provider import (
    default_model_id,
    estimate_cost_usd_cents,
    list_models,
    llm_enabled,
)

router = APIRouter(prefix="/api")

VALID_LEVELS = {"CONVERSATION", "AGENT_RUN", "SPAN", "TOOL", "GENERATION", "CHAIN"}
VALID_KINDS = {"structural", "llm_judge"}
# Structural checks have a real target shape, rather than a generic prompt that can adapt to a
# level. Letting users pair them arbitrarily created scores whose stated level disagreed with
# their address (for example a TOOL score with no observation_id), so the table had nowhere to
# render the result.
STRUCTURAL_LEVELS = {
    "run_outcome": "AGENT_RUN",
    "tool_success": "TOOL",
    "tool_consistency": "AGENT_RUN",
    "latency": "AGENT_RUN",
    "required_tools": "AGENT_RUN",
}


def _validate_evaluator(kind: str, level: str, config: dict[str, Any]) -> None:
    if kind == "structural":
        check = str(config.get("check") or "")
        required_level = STRUCTURAL_LEVELS.get(check)
        if required_level is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown structural check {check!r}; choose one of {sorted(STRUCTURAL_LEVELS)}",
            )
        if level != required_level:
            raise HTTPException(
                status_code=400,
                detail=f"structural check {check!r} must use level {required_level}",
            )
        return

    output_type = str(config.get("output_type") or "score").lower()
    if output_type not in OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"output_type must be one of {list(OUTPUT_TYPES)}",
        )
    execution_mode = str(config.get("execution_mode") or "batch").lower()
    if execution_mode not in {"batch", "sequential"}:
        raise HTTPException(status_code=400, detail="execution_mode must be 'batch' or 'sequential'")
    # Knobs the runner would otherwise trip over mid-grade (a bad value there doesn't crash the
    # pipeline — the per-evaluator catch eats it — but the column silently stops producing scores,
    # which is the worst way to learn your threshold was the string "0.6 or so").
    if config.get("threshold") is not None and not isinstance(config["threshold"], (int, float)):
        raise HTTPException(status_code=400, detail="threshold must be a number")
    if config.get("max_spans") is not None and (
        not isinstance(config["max_spans"], int) or config["max_spans"] < 1
    ):
        raise HTTPException(status_code=400, detail="max_spans must be a positive integer")
    span_types = config.get("span_types")
    if span_types is not None:
        allowed = {"SPAN", "TOOL", "GENERATION", "CHAIN"}
        if not isinstance(span_types, list) or not set(map(str, span_types)) <= allowed:
            raise HTTPException(
                status_code=400, detail=f"span_types must be a list drawn from {sorted(allowed)}"
            )
    depends_on = config.get("depends_on")
    if depends_on is not None and (
        not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on)
    ):
        raise HTTPException(status_code=400, detail="depends_on must be a list of score names")
    _validate_models(output_type, config)
    if output_type == "json" and config.get("output_schema") is not None:
        try:
            compiled = model_from_json_schema(config["output_schema"])
        except Exception:
            compiled = None
        # None also covers a schema the compiler understands but can't build a contract from —
        # the run path would silently fall back to free-form JSON, so reject it here instead.
        if compiled is None:
            raise HTTPException(
                status_code=400,
                detail="output_schema is not a usable JSON Schema (object with typed properties)",
            )


def _validate_models(output_type: str, config: dict[str, Any]) -> None:
    """A decision model (a classifier — TypeSafe Jev) answers typed questions, not rubrics, so it
    and the `decision_*` output types come as a pair; and a context fallback has to be a model
    that can read MORE than the primary, which only a text model on the chat path can."""
    model = str(config.get("model") or "").strip()
    wants_decision = decision.is_decision_output(output_type)
    if model and provider.is_decision_model(model) and not wants_decision:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{model} is a decision model: it answers one typed question instead of grading a "
                f"rubric, so output_type must be one of {list(decision.DECISION_OUTPUT_TYPES)}"
            ),
        )
    if wants_decision:
        if model and not provider.is_decision_model(model):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"output_type {output_type} needs a decision model (e.g. typesafe/jev-1.13); "
                    f"{model} is an LLM — use score/number/boolean/text/json with it"
                ),
            )
        if not model:
            raise HTTPException(
                status_code=400,
                detail=f"output_type {output_type} needs `model` set to a decision model (e.g. typesafe/jev-1.13)",
            )
        problem = decision.validate(config)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
    fallback = config.get("fallback_model")
    if fallback is not None:
        if not isinstance(fallback, str) or not fallback.strip():
            raise HTTPException(status_code=400, detail="fallback_model must be a model id, or omitted")
        if provider.is_decision_model(fallback):
            raise HTTPException(
                status_code=400,
                detail="fallback_model must be an LLM (it grades items too long for the column's model); decision models have small fixed contexts",
            )
        if fallback.strip() == model:
            raise HTTPException(status_code=400, detail="fallback_model must differ from the column's model")


def _stamp_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """Recompute `is_advanced` + `template_variables` from the judge prompt — the single server-
    side source of truth. A prompt containing any `@VARIABLE` makes the column advanced (routes to
    the template resolver); without one it's a plain rubric. Promptless (structural) configs pass
    through untouched."""
    prompt = config.get("prompt")
    if not isinstance(prompt, str):
        return config
    refs = extract_template_variables(prompt)
    return {**config, "template_variables": refs, "is_advanced": bool(refs)}


def _evaluator_dict(e: Evaluator) -> dict[str, Any]:
    return {
        "id": e.id,
        "name": e.name,
        "description": e.description,
        "kind": e.kind,
        "score_name": e.score_name,
        "level": e.level,
        "enabled": e.enabled,
        "target_agent": e.target_agent,
        "target_env": e.target_env,
        "sampling": e.sampling,
        "config": e.config or {},
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


class EvaluatorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    kind: str = "llm_judge"
    level: str = "AGENT_RUN"
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    score_name: str = Field(default="", max_length=80)


class EvaluatorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    level: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    target_agent: str | None = Field(default=None, max_length=80)
    target_env: str | None = Field(default=None, max_length=32)
    sampling: float | None = Field(default=None, ge=0.0, le=1.0)


class GenerateRequest(BaseModel):
    description: str = Field(min_length=3, max_length=2000)


class ResolveRequest(BaseModel):
    """Live-preview request: resolve an advanced `@VARIABLE` prompt against one real item."""

    prompt: str = Field(default="", max_length=20000)
    level: str = "AGENT_RUN"
    thread_id: str = ""
    trace_id: str = ""
    span_id: str = ""


@router.get("/evaluators")
async def list_evaluators(project_id: str = Depends(get_project_id)) -> list[dict]:
    def work():
        with SyncSessionLocal() as s:
            return [_evaluator_dict(e) for e in repo.evaluators_list(s, project_id)]

    return await run_in_threadpool(work)


@router.get("/evaluators/models")
async def list_judge_models(project_id: str = Depends(get_project_id)) -> dict:
    """The curated judge-model choices for the Add Column form (verified against OpenRouter
    when reachable) plus the project default used when a column doesn't pick one."""

    def work():
        with provider.use_project_key(project_id):
            return list_models()

    models = await run_in_threadpool(work)
    return {"default": default_model_id(), "models": models}


@router.get("/evaluators/cost")
async def evaluator_cost(
    days: int = 30, project_id: str = Depends(get_project_id)
) -> dict:
    """Per-evaluator LLM-judge cost over the last `days`: token counts + USD cents (priced from
    OpenRouter when reachable, else a static fallback table — see `provider.model_pricing`).
    Includes a project summary with `traces_in_window` so the UI can show $/1k-traces.

    Shape: `{evaluators: {<score_name>: {runs, input_tokens, output_tokens, total_tokens,
    cost_usd_cents, model}}, summary: {days, traces_in_window, total_runs, total_input_tokens,
    total_output_tokens, total_cost_usd_cents}}`."""
    days = max(1, min(int(days), 365))
    by_name = await async_reader.evaluator_cost(project_id, days)
    traces = await async_reader.traces_in_window(project_id, days)

    evaluators: dict[str, dict] = {}
    total_in = total_out = total_runs = total_cents = 0
    with provider.use_project_key(project_id):
        for name, c in by_name.items():
            cents = estimate_cost_usd_cents(c["model"], c["input_tokens"], c["output_tokens"])
            evaluators[name] = {**c, "cost_usd_cents": cents}
            total_runs += c["runs"]
            total_in += c["input_tokens"]
            total_out += c["output_tokens"]
            total_cents += cents
    return {
        "evaluators": evaluators,
        "summary": {
            "days": days,
            "traces_in_window": traces,
            "total_runs": total_runs,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cost_usd_cents": total_cents,
        },
    }


@router.get("/evaluators/templates")
async def list_templates(project_id: str = Depends(get_project_id)) -> list[dict]:
    """The browse library: every catalog template, flagged with whether this project already
    has it installed (matched by score_name)."""

    def work():
        with SyncSessionLocal() as s:
            installed = repo.evaluator_score_names(s, project_id)
        return [{**t, "installed": t["score_name"] in installed} for t in TEMPLATES]

    return await run_in_threadpool(work)


@router.get("/evaluators/template-variables/{level}")
async def template_variables(level: str, project_id: str = Depends(get_project_id)) -> list[dict]:
    """The advanced-mode `@VARIABLE`s available at `level` (name/description/type/props) — drives
    the editor's autocomplete + 'Available variables: N' count. Level-filtered server-side."""
    return variables_for_level_json(level)


@router.post("/evaluators/resolve")
async def resolve_prompt(
    body: ResolveRequest, project_id: str = Depends(get_project_id)
) -> dict:
    """Resolve an advanced `@VARIABLE` prompt against a real conversation/turn/step for the live
    preview — the SAME context builder + resolver the run path uses, minus the LLM call. Returns
    the resolved text plus which variables were used / missing (drives the green/amber badges)."""
    level = body.level if body.level in VALID_LEVELS else "AGENT_RUN"
    thread_id = body.thread_id or body.trace_id
    spans = await async_reader.thread_spans_full(project_id, thread_id) if thread_id else []
    wanted = extract_template_variables(body.prompt)
    # The rolling summary backs @ROLLING_SUMMARY and substitutes for @HISTORY/@MESSAGES so the
    # preview matches what the run path grades (run==preview parity); falls back to the raw
    # transcript when no summary exists.
    base_names = {w.split(".", 1)[0] for w in wanted}
    history_override = None
    if thread_id and ({"HISTORY", "MESSAGES", "ROLLING_SUMMARY"} & base_names):
        from tracely.services.rolling_summary_service import RollingSummaryService

        history_override = await run_in_threadpool(
            RollingSummaryService.history_override,
            project_id,
            thread_id,
            # same cut as the run path: a message/step judge reads the summary as of its own turn
            "" if level == "CONVERSATION" or not body.trace_id else body.trace_id,
        )
    declared_agents = None
    if thread_id and "LIST_AGENT" in base_names:
        from tracely.services.conversation_agents_service import ConversationAgentsService

        declared_agents = await run_in_threadpool(
            ConversationAgentsService.for_thread, project_id, thread_id
        )
    context = build_context(
        level,
        thread_spans=spans,
        current_trace_id=body.trace_id or thread_id,
        current_span_id=body.span_id or None,
        wanted_vars=wanted,
        history_override=history_override,
        declared_agents=declared_agents,
    )
    resolved = template_resolver.resolve(body.prompt, context)
    return {
        "resolved_prompt": resolved.resolved_text,
        "variables_used": resolved.variables_used,
        "variables_missing": resolved.variables_missing,
        "level": level,
    }


@router.post("/evaluators/generate")
async def generate_evaluator(
    body: GenerateRequest, project_id: str = Depends(get_project_id)
) -> dict:
    """Natural-language description → a draft evaluator config (the UI pre-fills the manual
    form with it; nothing is persisted here)."""
    with provider.use_project_key(project_id):
        enabled = llm_enabled()
    if not enabled:
        raise HTTPException(
            status_code=503,
            detail="AI generation needs this workspace's OpenRouter key. Add one in "
            "Settings -> OpenRouter key.",
        )
    try:
        return await run_in_threadpool(generate_evaluator_config, body.description, project_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"generation failed: {exc}") from None


@router.post("/evaluators")
async def create_evaluator(
    body: EvaluatorCreate, project_id: str = Depends(get_project_id)
) -> dict:
    if body.kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(VALID_KINDS)}")
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail=f"level must be one of {sorted(VALID_LEVELS)}")

    config = _stamp_advanced(body.config or {})
    _validate_evaluator(body.kind, body.level, config)

    def work():
        with SyncSessionLocal() as s:
            e = repo.evaluator_create(
                s, project_id,
                name=body.name, description=body.description, kind=body.kind,
                level=body.level, enabled=body.enabled, config=config,
                score_name=body.score_name,
            )
            return _evaluator_dict(e)

    return await run_in_threadpool(work)


@router.patch("/evaluators/{evaluator_id}")
async def update_evaluator(
    evaluator_id: str, body: EvaluatorUpdate, project_id: str = Depends(get_project_id)
) -> dict:
    if body.level is not None and body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail=f"level must be one of {sorted(VALID_LEVELS)}")

    def work():
        with SyncSessionLocal() as s:
            patch = body.model_dump(exclude_unset=True)
            # Only recompute advanced-ness when the config (hence the prompt) is actually being
            # changed — an untouched config must not be clobbered by exclude_unset.
            if isinstance(patch.get("config"), dict):
                patch["config"] = _stamp_advanced(patch["config"])
            existing = repo.evaluator_get(s, project_id, evaluator_id)
            if existing is None:
                return None
            _validate_evaluator(
                existing.kind,
                patch.get("level", existing.level),
                patch.get("config", existing.config or {}),
            )
            e = repo.evaluator_update(s, project_id, evaluator_id, patch)
            return None if e is None else _evaluator_dict(e)

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="evaluator not found")
    return res


@router.delete("/evaluators/{evaluator_id}", dependencies=[Depends(require_user)])
async def delete_evaluator(
    evaluator_id: str, project_id: str = Depends(get_project_id)
) -> dict:
    def work():
        with SyncSessionLocal() as s:
            return repo.evaluator_delete(s, project_id, evaluator_id)

    ok = await run_in_threadpool(work)
    if not ok:
        raise HTTPException(status_code=404, detail="evaluator not found")
    return {"deleted": evaluator_id}
