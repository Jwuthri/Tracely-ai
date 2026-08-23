"""Deterministic cross-metric statistics for meta-analysis.

Pure functions over a `{metric -> {conversation_id -> value}}` matrix (one aggregated numeric
value per metric per conversation). No LLM, no I/O — so the correlations and outliers a run
reports are stable and reproducible regardless of the prose the model writes around them.

Spearman is implemented on tie-averaged ranks (rank → Pearson) so we don't take a scipy
dependency just for `spearmanr`. We deliberately report the sample size `n` instead of a p-value:
a real p-value needs the Student-t CDF, and shipping an approximate one would be worse than
honestly reporting how many shared points the coefficient rests on.
"""

from __future__ import annotations

import math

import numpy as np

# Minimums (mirror the source spec): a correlation needs at least this many shared conversations;
# an outlier metric needs at least this many values to have a meaningful mean/spread.
MIN_CORR_POINTS = 3
MIN_OUTLIER_POINTS = 2
# |z| above this flags an outlier; the high/medium cutoffs grade its severity.
OUTLIER_Z = 2.0
SEVERITY_HIGH_Z = 3.0
SEVERITY_MED_Z = 2.5

Matrix = dict[str, dict[str, float]]


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Tie-averaged ranks (like scipy.stats.rankdata, 'average' method)."""
    sorter = np.argsort(a, kind="mergesort")
    inv = np.empty(len(a), dtype=np.intp)
    inv[sorter] = np.arange(len(a), dtype=np.intp)
    a_sorted = a[sorter]
    obs = np.r_[True, a_sorted[1:] != a_sorted[:-1]]
    dense = obs.cumsum()[inv]
    counts = np.r_[np.nonzero(obs)[0], len(a)]
    return 0.5 * (counts[dense] + counts[dense - 1] + 1)


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    xc = x - x.mean()
    yc = y - y.mean()
    denom = math.sqrt(float((xc * xc).sum()) * float((yc * yc).sum()))
    return float((xc * yc).sum() / denom) if denom else 0.0


def _spearman(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation = Pearson on the tie-averaged ranks. 0 when either side is
    constant (no spread → correlation undefined; reported as 0, not NaN)."""
    rx = _rankdata(np.asarray(x, dtype=float))
    ry = _rankdata(np.asarray(y, dtype=float))
    return _pearson(rx, ry)


def spearman_correlations(matrix: Matrix) -> list[dict]:
    """Every metric pair sharing ≥ `MIN_CORR_POINTS` conversations, as
    `{metric_a, metric_b, coefficient, n}`, sorted by |coefficient| descending. Constant-valued
    pairs are dropped (coefficient 0 carries no signal)."""
    metrics = sorted(matrix)
    out: list[dict] = []
    for i in range(len(metrics)):
        for j in range(i + 1, len(metrics)):
            ma, mb = metrics[i], metrics[j]
            shared = sorted(set(matrix[ma]) & set(matrix[mb]))
            if len(shared) < MIN_CORR_POINTS:
                continue
            xs = [matrix[ma][c] for c in shared]
            ys = [matrix[mb][c] for c in shared]
            if len(set(xs)) < 2 or len(set(ys)) < 2:
                continue  # one side constant → undefined correlation
            coef = _spearman(xs, ys)
            out.append(
                {
                    "metric_a": ma,
                    "metric_b": mb,
                    "coefficient": round(coef, 3),
                    "p_value": None,
                    "n": len(shared),
                }
            )
    out.sort(key=lambda c: abs(c["coefficient"]), reverse=True)
    return out


def _severity(z: float) -> str:
    az = abs(z)
    if az >= SEVERITY_HIGH_Z:
        return "high"
    if az >= SEVERITY_MED_Z:
        return "medium"
    return "low"


def zscore_outliers(matrix: Matrix) -> list[dict]:
    """Conversations that are statistical outliers on one or more metrics (|z| > `OUTLIER_Z`).

    Per metric, z = (value − mean) / std over its conversations (needs ≥ `MIN_OUTLIER_POINTS`
    values and non-zero spread). Results are grouped by conversation:
    `{conversation_id, metrics_affected, z_scores, severity, reason}` — severity is graded off the
    largest |z| the conversation hit, sorted most-extreme first."""
    by_conv: dict[str, dict[str, float]] = {}
    for metric, conv_vals in matrix.items():
        vals = list(conv_vals.values())
        if len(vals) < MIN_OUTLIER_POINTS:
            continue
        arr = np.asarray(vals, dtype=float)
        mean = float(arr.mean())
        std = float(arr.std())
        if std == 0.0:
            continue
        for conv, v in conv_vals.items():
            z = (v - mean) / std
            if abs(z) > OUTLIER_Z:
                by_conv.setdefault(conv, {})[metric] = round(z, 2)

    outliers: list[dict] = []
    for conv, zmap in by_conv.items():
        worst = max(zmap.values(), key=abs)
        metrics_affected = sorted(zmap)
        direction = "above" if worst > 0 else "below"
        outliers.append(
            {
                "conversation_id": conv,
                "metrics_affected": metrics_affected,
                "z_scores": zmap,
                "severity": _severity(worst),
                "reason": (
                    f"{direction}-average on {', '.join(metrics_affected)} "
                    f"(max |z|={abs(worst):.1f})"
                ),
            }
        )
    outliers.sort(key=lambda o: max(abs(z) for z in o["z_scores"].values()), reverse=True)
    return outliers


def build_matrix(rows: list[dict]) -> Matrix:
    """Collapse flat score rows into the per-metric, per-conversation numeric matrix.

    `rows` are `{conversation_id, metric_name, value, ...}` (value may be None for text-only
    scores — skipped). Multiple values for the same (metric, conversation) — e.g. a step metric
    over many spans, or a turn metric over many turns — are averaged into one point.
    """
    buckets: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        val = r.get("value")
        conv = r.get("conversation_id")
        metric = r.get("metric_name")
        if val is None or not conv or not metric:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        buckets.setdefault(metric, {}).setdefault(conv, []).append(f)
    return {
        metric: {conv: sum(vs) / len(vs) for conv, vs in conv_vals.items()}
        for metric, conv_vals in buckets.items()
    }


def metric_stats(matrix: Matrix) -> list[dict]:
    """Per-metric snapshot `{metric, n, mean, std, min, max}`, sorted by name — stored on every
    analysis so the next run can report how each metric MOVED (`mean_shifts`)."""
    out: list[dict] = []
    for metric in sorted(matrix):
        arr = np.asarray(list(matrix[metric].values()), dtype=float)
        out.append(
            {
                "metric": metric,
                "n": int(len(arr)),
                "mean": round(float(arr.mean()), 4),
                "std": round(float(arr.std()), 4),
                "min": round(float(arr.min()), 4),
                "max": round(float(arr.max()), 4),
            }
        )
    return out


def mean_shifts(current: list[dict], previous: list[dict] | None) -> list[dict]:
    """How each metric's mean moved since the previous analysis: `{metric, prev_mean, mean,
    delta, n, prev_n}` for metrics present in BOTH snapshots, sorted by |delta| descending.
    A metric that appeared or disappeared is not a shift — it simply isn't listed."""
    prev = {s["metric"]: s for s in previous or []}
    shifts: list[dict] = []
    for cur in current:
        p = prev.get(cur["metric"])
        if p is None:
            continue
        shifts.append(
            {
                "metric": cur["metric"],
                "prev_mean": p["mean"],
                "mean": cur["mean"],
                "delta": round(cur["mean"] - p["mean"], 4),
                "n": cur["n"],
                "prev_n": p["n"],
            }
        )
    shifts.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return shifts
