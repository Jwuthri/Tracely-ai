"""Meta-analysis orchestration: gather → precompute stats → LLM synthesis → merge → persist.

The async ClickHouse gather happens in the router (`async_reader.agent_score_rows`); this service
is the sync compute+persist half, run in a threadpool. The statistics
(`domain.analysis.statistics`) are deterministic and authoritative — the LLM synthesis
(`infrastructure.llm.meta_analysis_agent`) adds interpretation/patterns/recommendations on top,
and we MERGE the precomputed correlations/outliers back in so the numbers are never lost or
hallucinated. With no LLM credential configured the run still succeeds, returning the stats plus a
templated summary.
"""

from __future__ import annotations

import structlog

from tracely.domain.analysis import statistics
from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.meta_analysis_agent import MetaAnalysisOutput, synthesize

log = structlog.get_logger()


class MetaAnalysisService:
    @classmethod
    def analyze_and_save(cls, project_id: str, agent_id: str, rows: list[dict]) -> dict:
        """Compute + synthesize + persist a meta-analysis for an agent's score rows. Returns the
        response dict the router serializes."""
        agent_id = agent_id or ""
        with SyncSessionLocal() as s:
            agent = repositories.agent_in_project(s, project_id, agent_id) if agent_id else None
            agent_slug = agent.slug if agent else ""
            prev = repositories.meta_analysis_latest_for_agent(s, project_id, agent_id)
            prev_stats = (prev.result or {}).get("metric_stats") if prev else None

        with provider.use_project_key(project_id):
            result, meta = cls._analyze(agent_id, agent_slug, rows, prev_stats=prev_stats)

        with SyncSessionLocal() as s:
            row = repositories.meta_analysis_create(
                s, project_id, agent_id=agent_id, result=result, meta=meta
            )
            return _to_response(row)

    @classmethod
    def _analyze(
        cls,
        agent_id: str,
        agent_slug: str,
        rows: list[dict],
        prev_stats: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        matrix = statistics.build_matrix(rows)
        metrics = sorted(matrix)
        conversations = sorted({c for conv_vals in matrix.values() for c in conv_vals})
        correlations = statistics.spearman_correlations(matrix)
        outliers = statistics.zscore_outliers(matrix)
        stats = statistics.metric_stats(matrix)
        shifts = statistics.mean_shifts(stats, prev_stats)

        llm_out: MetaAnalysisOutput | None = None
        if provider.llm_enabled() and metrics:
            try:
                llm_out = synthesize(
                    _build_prompt(matrix, correlations, outliers, agent_slug, shifts)
                )
            except Exception as exc:  # a failed synthesis degrades to stats-only, never errors
                log.warning("meta_analysis_synthesis_failed", error=str(exc))

        result = _merge(matrix, metrics, conversations, correlations, outliers, llm_out)
        result["metric_stats"] = stats
        result["metric_shifts"] = shifts
        meta = {
            "model": (settings_model() if llm_out else ""),
            "agent_id": agent_id,
            "agent_slug": agent_slug,
            "metrics_analyzed": len(metrics),
            "conversations_analyzed": len(conversations),
            "n_results": len(rows),
            "metrics": metrics,
            "llm": bool(llm_out),
        }
        return result, meta


def settings_model() -> str:
    from tracely.config import settings

    return settings.meta_analysis_model


def _stat_summary(metric: str, conv_vals: dict[str, float]) -> str:
    import numpy as np

    arr = np.asarray(list(conv_vals.values()), dtype=float)
    return (
        f"- {metric}: n={len(arr)} conversations, mean={arr.mean():.3f}, "
        f"min={arr.min():.3f}, max={arr.max():.3f}, std={arr.std():.3f}"
    )


def _build_prompt(
    matrix: statistics.Matrix,
    correlations: list[dict],
    outliers: list[dict],
    agent_slug: str,
    shifts: list[dict] | None = None,
) -> str:
    lines: list[str] = []
    who = f" for agent '{agent_slug}'" if agent_slug else " across the whole project"
    lines.append(f"Evaluation metric results{who}.")
    lines.append("")
    lines.append("PER-METRIC STATISTICS (one aggregated value per conversation):")
    for metric in sorted(matrix):
        lines.append(_stat_summary(metric, matrix[metric]))
    lines.append("")
    if correlations:
        lines.append("PRECOMPUTED SPEARMAN CORRELATIONS (authoritative — interpret, don't change):")
        for c in correlations[:25]:
            lines.append(
                f"- {c['metric_a']} ↔ {c['metric_b']}: coef={c['coefficient']} (n={c['n']})"
            )
    else:
        lines.append("PRECOMPUTED CORRELATIONS: none (no metric pair shared enough conversations).")
    lines.append("")
    if outliers:
        lines.append("PRECOMPUTED OUTLIER CONVERSATIONS (z-score):")
        for o in outliers[:25]:
            zs = ", ".join(f"{m}={z}" for m, z in o["z_scores"].items())
            lines.append(
                f"- conversation {o['conversation_id']} [{o['severity']}]: {zs}"
            )
    else:
        lines.append("PRECOMPUTED OUTLIERS: none.")
    lines.append("")
    moved = [s for s in (shifts or []) if s["delta"] != 0]
    if moved:
        lines.append("SINCE THE PREVIOUS ANALYSIS (mean shifts, current − previous):")
        for s in moved[:15]:
            lines.append(
                f"- {s['metric']}: {s['prev_mean']} → {s['mean']} (Δ {s['delta']:+})"
            )
        lines.append("")
    lines.append(
        "Write the meta-analysis: patterns, correlation interpretations, outlier explanations, "
        "recommendations, a summary, and a confidence score."
    )
    return "\n".join(lines)


def _interp(coef: float) -> str:
    a = abs(coef)
    direction = "positive" if coef >= 0 else "negative"
    strength = "strong" if a >= 0.7 else "moderate" if a >= 0.4 else "weak"
    return f"{strength} {direction} association"


def _merge(
    matrix: statistics.Matrix,
    metrics: list[str],
    conversations: list[str],
    correlations: list[dict],
    outliers: list[dict],
    llm_out: MetaAnalysisOutput | None,
) -> dict:
    """Deterministic stats are authoritative; LLM prose is layered on where it matches."""
    # correlations: keep precomputed coefficient + n; borrow the LLM's interpretation if the
    # (unordered) metric pair matches, else generate a plain-language one.
    llm_interp: dict[frozenset[str], str] = {}
    if llm_out:
        for c in llm_out.correlations:
            llm_interp[frozenset({c.metric_a, c.metric_b})] = c.interpretation
    merged_corr = [
        {
            "metric_a": c["metric_a"],
            "metric_b": c["metric_b"],
            "coefficient": c["coefficient"],
            "p_value": c.get("p_value"),
            "n": c["n"],
            "interpretation": llm_interp.get(
                frozenset({c["metric_a"], c["metric_b"]}), _interp(c["coefficient"])
            ),
        }
        for c in correlations
    ]

    # outliers: keep precomputed addressing/z-scores/severity; borrow the LLM's reason by id.
    llm_reason = {o.conversation_id: o.reason for o in (llm_out.outliers if llm_out else [])}
    merged_out = [
        {
            "conversation_id": o["conversation_id"],
            "metrics_affected": o["metrics_affected"],
            "z_scores": o["z_scores"],
            "severity": o["severity"],
            "reason": llm_reason.get(o["conversation_id"], o["reason"]),
        }
        for o in outliers
    ]

    if llm_out:
        patterns = [p.model_dump() for p in llm_out.patterns]
        recommendations = llm_out.recommendations
        summary = llm_out.summary
        confidence = round(max(0.0, min(1.0, llm_out.confidence)), 2)
    else:
        patterns = []
        recommendations = _fallback_recommendations(merged_corr, merged_out)
        summary = _fallback_summary(metrics, conversations, merged_corr, merged_out)
        confidence = _fallback_confidence(len(metrics), len(conversations))

    return {
        "patterns": patterns,
        "correlations": merged_corr,
        "outliers": merged_out,
        "recommendations": recommendations,
        "summary": summary,
        "confidence": confidence,
        "metrics_analyzed": len(metrics),
        "conversations_analyzed": len(conversations),
    }


def _fallback_summary(
    metrics: list[str], conversations: list[str], corr: list[dict], out: list[dict]
) -> str:
    parts = [
        f"Analyzed {len(metrics)} metric(s) across {len(conversations)} conversation(s).",
    ]
    if corr:
        top = corr[0]
        parts.append(
            f"Strongest association: {top['metric_a']} and {top['metric_b']} "
            f"({top['coefficient']:+.2f}, {top['interpretation']})."
        )
    if out:
        parts.append(f"{len(out)} outlier conversation(s) flagged by z-score.")
    parts.append("(LLM synthesis unavailable — showing computed statistics only.)")
    return " ".join(parts)


def _fallback_recommendations(corr: list[dict], out: list[dict]) -> list[str]:
    recs: list[str] = []
    for c in corr[:3]:
        if abs(c["coefficient"]) >= 0.4:
            recs.append(
                f"Investigate the link between {c['metric_a']} and {c['metric_b']} "
                f"({c['coefficient']:+.2f})."
            )
    if out:
        recs.append(
            f"Review the {len(out)} flagged outlier conversation(s) for root causes."
        )
    return recs


def _fallback_confidence(n_metrics: int, n_conv: int) -> float:
    if not n_metrics or not n_conv:
        return 0.0
    return round(min(1.0, n_conv / 40.0) * min(1.0, n_metrics / 4.0), 2)


def _to_response(row) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "agent_slug": (row.meta or {}).get("agent_slug", ""),
        "analysis_type": row.analysis_type,
        "status": "complete",
        "result": row.result,
        "meta": row.meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
