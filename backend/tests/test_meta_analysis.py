"""Meta-analysis: deterministic statistics + the LLM-merge (stats stay authoritative)."""

from __future__ import annotations

import pytest

from tracely.domain.analysis import statistics as st
from tracely.infrastructure.llm.meta_analysis_agent import (
    Correlation,
    MetaAnalysisOutput,
    Pattern,
)
from tracely.services import meta_analysis_service as svc
from tracely.services.meta_analysis_service import MetaAnalysisService


def _rows():
    rows = []
    x = {"c1": 0.1, "c2": 0.3, "c3": 0.5, "c4": 0.7, "c5": 0.9, "c6": 5.0}  # c6 outlier
    y = {"c1": 0.1, "c2": 0.3, "c3": 0.5, "c4": 0.7, "c5": 0.9}  # tracks x
    z = {"c1": 0.9, "c2": 0.7, "c3": 0.5, "c4": 0.3, "c5": 0.1}  # anti-correlated
    for c, v in x.items():
        rows.append({"conversation_id": c, "metric_name": "x", "value": v})
    for c, v in y.items():
        rows.append({"conversation_id": c, "metric_name": "y", "value": v})
    for c, v in z.items():
        rows.append({"conversation_id": c, "metric_name": "z", "value": v})
    return rows


def test_build_matrix_averages_duplicates():
    rows = [
        {"conversation_id": "c1", "metric_name": "m", "value": 0.2},
        {"conversation_id": "c1", "metric_name": "m", "value": 0.4},
        {"conversation_id": "c2", "metric_name": "m", "value": 1.0},
        {"conversation_id": "c3", "metric_name": "m", "value": None},  # skipped
    ]
    m = st.build_matrix(rows)
    assert m["m"]["c1"] == pytest.approx(0.3)  # averaged
    assert m["m"]["c2"] == 1.0
    assert "c3" not in m["m"]


def test_spearman_perfect_and_sorted():
    m = st.build_matrix(_rows())
    corr = st.spearman_correlations(m)
    by_pair = {frozenset((c["metric_a"], c["metric_b"])): c for c in corr}
    assert by_pair[frozenset(("x", "y"))]["coefficient"] == 1.0
    assert by_pair[frozenset(("x", "z"))]["coefficient"] == -1.0
    # sorted by |coefficient| descending
    coefs = [abs(c["coefficient"]) for c in corr]
    assert coefs == sorted(coefs, reverse=True)


def test_correlation_needs_min_points():
    m = {"a": {"c1": 1.0, "c2": 2.0}, "b": {"c1": 1.0, "c2": 2.0}}  # only 2 shared
    assert st.spearman_correlations(m) == []


def test_zscore_outlier_detected():
    m = st.build_matrix(_rows())
    outliers = st.zscore_outliers(m)
    ids = {o["conversation_id"] for o in outliers}
    assert "c6" in ids
    c6 = next(o for o in outliers if o["conversation_id"] == "c6")
    assert "x" in c6["metrics_affected"]
    assert c6["severity"] in ("low", "medium", "high")


def test_analyze_stats_only_when_no_llm(monkeypatch):
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: False)
    result, meta = MetaAnalysisService._analyze("", "", _rows())
    assert result["metrics_analyzed"] == 3
    assert result["conversations_analyzed"] == 6
    assert result["correlations"], "deterministic correlations must survive without an LLM"
    assert result["patterns"] == []
    assert "stats only" in result["summary"].lower() or "statistics only" in result["summary"].lower()
    assert meta["llm"] is False


def test_analyze_merges_llm_but_keeps_stats(monkeypatch):
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)

    def fake_synthesize(prompt: str) -> MetaAnalysisOutput:
        return MetaAnalysisOutput(
            patterns=[Pattern(description="x and y move together", evidence="strong corr", affected_metrics=["x", "y"])],
            correlations=[Correlation(metric_a="x", metric_b="y", coefficient=0.123, interpretation="they rise together")],
            outliers=[],
            recommendations=["look at c6"],
            summary="key findings",
            confidence=0.8,
        )

    monkeypatch.setattr(svc, "synthesize", fake_synthesize)
    result, meta = MetaAnalysisService._analyze("aid", "weather", _rows())

    assert meta["llm"] is True
    assert result["summary"] == "key findings"
    assert result["patterns"][0]["description"] == "x and y move together"
    # deterministic coefficient wins; the LLM only contributes interpretation
    xy = next(c for c in result["correlations"] if {c["metric_a"], c["metric_b"]} == {"x", "y"})
    assert xy["coefficient"] == 1.0
    assert xy["interpretation"] == "they rise together"


def test_metric_stats_and_mean_shifts():
    m = st.build_matrix(_rows())
    stats = st.metric_stats(m)
    x = next(s for s in stats if s["metric"] == "x")
    assert x["n"] == 6 and x["min"] == 0.1 and x["max"] == 5.0

    prev = [dict(s) for s in stats]
    prev_x = next(s for s in prev if s["metric"] == "x")
    prev_x["mean"] = round(prev_x["mean"] - 0.5, 4)
    prev.append({"metric": "gone", "n": 3, "mean": 1.0, "std": 0.0, "min": 1.0, "max": 1.0})
    shifts = st.mean_shifts(stats, prev)
    # only shared metrics; the biggest mover first; "gone" is not a shift
    assert [s["metric"] for s in shifts][0] == "x"
    assert all(s["metric"] != "gone" for s in shifts)
    assert next(s for s in shifts if s["metric"] == "x")["delta"] == 0.5
    assert st.mean_shifts(stats, None) == []


def test_analyze_reports_stats_snapshot_and_shifts(monkeypatch):
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: False)
    first, _ = MetaAnalysisService._analyze("", "", _rows())
    assert {s["metric"] for s in first["metric_stats"]} == {"x", "y", "z"}
    assert first["metric_shifts"] == []  # nothing to compare against

    prev = [dict(s) for s in first["metric_stats"]]
    prev[0]["mean"] = round(prev[0]["mean"] + 0.25, 4)
    second, _ = MetaAnalysisService._analyze("", "", _rows(), prev_stats=prev)
    moved = next(s for s in second["metric_shifts"] if s["metric"] == prev[0]["metric"])
    assert moved["delta"] == -0.25  # current − previous


def test_ops_rows_flow_through_the_matrix():
    """Operational rows use the same flat shape as score rows, so the matrix treats them as
    metrics: per-turn values average per conversation, `ops.turns` arrives once per thread."""
    rows = [
        {"conversation_id": "t1", "metric_name": "ops.latency_ms", "value": 100.0},
        {"conversation_id": "t1", "metric_name": "ops.latency_ms", "value": 300.0},
        {"conversation_id": "t1", "metric_name": "ops.turns", "value": 2.0},
    ]
    m = st.build_matrix(rows)
    assert m["ops.latency_ms"]["t1"] == 200.0
    assert m["ops.turns"]["t1"] == 2.0
