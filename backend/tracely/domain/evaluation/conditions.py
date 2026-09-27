"""Conditional columns: `config.run_if` — run a column only when its dependencies came out a
certain way. Pure.

`depends_on` already orders columns and hands the dependent its prerequisites' results. `run_if`
adds the gate: "explain the answer with an LLM, but only on turns the Jev intent column labelled
`refund`" or "only where Answer quality FAILED". Cheap classifier first, expensive judge only
where it matters.

    run_if = [                                   # every condition must hold (AND)
      {"column": "tracely.run.intent", "field": "label",   "op": "in",  "values": ["refund"]},
      {"column": "tracely.run.quality", "field": "verdict", "op": "in",  "values": ["FAIL"]},
      {"column": "custom.helpfulness",  "field": "value",   "op": "lt",  "value": 0.5},
    ]

- `field: "verdict"` — PASS / FAIL, or NONE for a verdict-less (informational) result.
- `field: "label"`   — a multi-class label, or ANY of a multi-label column's labels; also a json
  column's first string field. `in` / `not_in` against `values`.
- `field: "value"`   — the numeric result (P(yes) of a binary, a score, a confidence).
  `gte` / `gt` / `lte` / `lt` against `value`.

A dependency with NO result for the item (it didn't apply, was skipped, or failed) makes the
condition false — the dependent doesn't run on evidence that doesn't exist. Every `column` must
also be in `depends_on` (the router adds it), which is what makes it run first and hands its
result over (`evaluation_service._inject_dependencies`). The payload each condition reads is
`results.chain_payload` — the same object the dependent's prompt receives.
"""

from __future__ import annotations

from typing import Any

FIELDS = ("verdict", "label", "value")
SET_OPS = ("in", "not_in")
NUM_OPS = ("gte", "gt", "lte", "lt")
VERDICTS = ("PASS", "FAIL", "NONE")
MAX_CONDITIONS = 5
_OP_WORDS = {"in": "is", "not_in": "is not", "gte": "≥", "gt": ">", "lte": "≤", "lt": "<"}


def validate(run_if: Any) -> str | None:
    """The first problem with a `run_if`, phrased for whoever sent it, or None."""
    if run_if is None:
        return None
    if not isinstance(run_if, list) or not run_if:
        return "run_if must be a non-empty list of conditions, e.g. " \
               '[{"column": "tracely.run.intent", "field": "label", "op": "in", "values": ["refund"]}]'
    if len(run_if) > MAX_CONDITIONS:
        return f"run_if takes at most {MAX_CONDITIONS} conditions"
    for i, c in enumerate(run_if):
        where = f"run_if[{i}]"
        if not isinstance(c, dict) or not str(c.get("column") or "").strip():
            return f"{where} needs `column`: the score_name of the column it reads"
        field, op = c.get("field"), c.get("op")
        if field not in FIELDS:
            return f"{where}.field must be one of {list(FIELDS)}"
        if field == "value":
            if op not in NUM_OPS:
                return f"{where}: a value condition's op must be one of {list(NUM_OPS)}"
            v = c.get("value")
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return f"{where}.value must be a number"
        else:
            if op not in SET_OPS:
                return f"{where}: a {field} condition's op must be one of {list(SET_OPS)}"
            values = c.get("values")
            if not isinstance(values, list) or not values or not all(isinstance(x, str) and x for x in values):
                return f"{where}.values must be a non-empty list of strings"
            if field == "verdict" and any(v.upper() not in VERDICTS for v in values):
                return f"{where}.values for a verdict condition are drawn from {list(VERDICTS)}"
    return None


def columns(run_if: Any) -> list[str]:
    """The score_names a `run_if` reads — each must run first (`depends_on`)."""
    if not isinstance(run_if, list):
        return []
    return list(dict.fromkeys(str(c["column"]) for c in run_if if isinstance(c, dict) and c.get("column")))


def _labels(payload: dict) -> list[str]:
    """The labels a dependency result carries: a decision column's `label` (a multi-label
    column's comma-joined fired set, split), else a json column's first short string field."""
    label = payload.get("label")
    if isinstance(label, str) and label:
        return [x.strip() for x in label.split(",") if x.strip() and x.strip() != "none"]
    for k, v in payload.items():
        if k not in ("verdict", "reason") and isinstance(v, str) and v and len(v) <= 64:
            return [v]
    return []


def _one(cond: dict, payload: dict) -> tuple[bool, str]:
    field, op = cond["field"], cond["op"]
    if field == "verdict":
        actual = str(payload.get("verdict") or "NONE").upper()
        wanted = {v.upper() for v in cond["values"]}
        hit = actual in wanted
        return (hit if op == "in" else not hit), actual
    if field == "label":
        got = _labels(payload)
        hit = bool(set(got) & set(cond["values"]))
        return (hit if op == "in" else not hit), ", ".join(got) or "none"
    v = payload.get("value", payload.get("score"))
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False, "no value"
    t = float(cond["value"])
    ok = {"gte": v >= t, "gt": v > t, "lte": v <= t, "lt": v < t}[op]
    return ok, f"{float(v):g}"


def evaluate(run_if: Any, deps: dict[str, Any]) -> tuple[bool, str]:
    """Whether every condition holds against `deps` (`{score_name: payload | [payloads]}`, as
    `llm_judge._deps` flattens them for one item), plus the sentence a skipped cell shows. No
    `run_if` = always runs. A list of payloads (several results for one item) passes a condition
    when ANY of them does."""
    if not run_if:
        return True, ""
    for cond in run_if:
        name = str(cond.get("column"))
        raw = deps.get(name)
        payloads = [p for p in (raw if isinstance(raw, list) else [raw]) if isinstance(p, dict)]
        if not payloads:
            return False, f"Not run: `{name}` has no result for this item."
        outcomes = [_one(cond, p) for p in payloads]
        if not any(ok for ok, _ in outcomes):
            actual = outcomes[0][1]
            target = cond.get("values") if cond["field"] != "value" else cond.get("value")
            shown = ", ".join(target) if isinstance(target, list) else target
            return False, (
                f"Not run: {name} {cond['field']} is {actual} "
                f"(runs when {cond['field']} {_OP_WORDS[cond['op']]} {shown})."
            )
    return True, ""
