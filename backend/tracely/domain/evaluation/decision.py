"""Decision-model columns: the questions a classifier answers, and its answers as a score. Pure.

A decision model (TypeSafe Jev on OpenRouter's Decisions API) is not a judge that reads a rubric
and writes a verdict. It reads a `state` (the graded item: the same body an LLM judge would get)
and answers typed questions with calibrated probabilities. So a decision column stores a question
instead of a rubric, and the output type is the classification task:

  decision_binary     → yes/no. `criteria = {"true": …, "false": …}`. One Jev `noul`.
                        NUMERIC value = P(yes); PASS iff (P ≥ threshold) matches `pass_when`
                        ("yes" by default; "no" keeps "Did the agent hallucinate?" literal instead
                        of inverting the criteria, which TypeSafe documents as degrading accuracy).
  decision_multiclass → exactly one of 2–255 labels. `criteria = {label: description | null}`.
                        One Jev `choice` (a softmax over the labels). CATEGORICAL string_value =
                        the label, value = confidence. FAIL iff it is in `fail_options`; PASS when
                        fail options exist and it isn't one; no verdict when none are marked.
  decision_multilabel → any subset of 2–50 labels. Same `criteria` shape. Jev has no native
                        multi-label type, so it is TypeSafe's fan-out pattern: one `noul` per label,
                        all in ONE request (answered in parallel, each against the same state, for
                        barely more than one question costs). A label fires at P ≥ threshold.
                        CATEGORICAL string_value = the fired labels ("none" if nothing fired);
                        FAIL iff any fired label is in `fail_options`.

Jev's third primitive, `score` (ordered levels), is deliberately not offered: it is a choice over
levels plus the probability-weighted level index — no information a multi-class column lacks.

The column's `fallback_model` (an LLM) answers the SAME questions when the item overflows the
decision model's context (`domain/evaluation/budget.py`): `fallback_schema` constrains it to the
same answer space and `fallback_answers` reshapes its reply as Decisions-API answers, so
`answer_fields` is the single mapping to a score and a column never changes data type because one
item happened to be long.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

BINARY, MULTICLASS, MULTILABEL = "decision_binary", "decision_multiclass", "decision_multilabel"
DECISION_OUTPUT_TYPES = (BINARY, MULTICLASS, MULTILABEL)

MAX_MULTICLASS_LABELS = 255  # Jev's per-choice limit
MAX_MULTILABEL_LABELS = 50  # one question each; TypeSafe's 64k covers state + ALL questions
DEFAULT_THRESHOLD = 0.5


def is_decision_output(output_type: Any) -> bool:
    return str(output_type or "").lower() in DECISION_OUTPUT_TYPES


def _output_type(config: dict) -> str:
    return str(config.get("output_type") or "").lower()


def _labels(config: dict) -> list[str]:
    crit = config.get("criteria")
    return [str(k) for k in crit] if isinstance(crit, dict) else []


def _desc(v: Any) -> Any:
    return None if v in (None, "") else v


def validate(config: dict) -> str | None:
    """The first problem with a decision column's config, phrased for whoever sent it (the UI,
    the MCP server, the assistant — all of which correct themselves from this text). None = OK."""
    ot = _output_type(config)
    if ot not in DECISION_OUTPUT_TYPES:
        return f"output_type must be one of {', '.join(DECISION_OUTPUT_TYPES)} for a decision model"
    if not str(config.get("question") or "").strip():
        return "question is required: the yes/no or classification question the model answers"
    t = config.get("threshold", DEFAULT_THRESHOLD)
    if ot != MULTICLASS and (
        not isinstance(t, (int, float)) or isinstance(t, bool) or not 0 < float(t) < 1
    ):
        return f"threshold must be a probability strictly between 0 and 1 for {ot}"
    crit = config.get("criteria")
    if ot == BINARY:
        if (
            not isinstance(crit, dict)
            or not str(crit.get("true") or "").strip()
            or not str(crit.get("false") or "").strip()
        ):
            return 'criteria must be {"true": "<what yes means>", "false": "<what no means>"} for decision_binary'
        if str(config.get("pass_when") or "yes") not in ("yes", "no"):
            return 'pass_when must be "yes" or "no"'
        return None
    if not isinstance(crit, dict):
        return 'criteria must map each label to a description (or null), e.g. {"billing": "Payments, refunds"}'
    labels = [k.strip() for k in _labels(config)]
    cap = MAX_MULTICLASS_LABELS if ot == MULTICLASS else MAX_MULTILABEL_LABELS
    if not 2 <= len(labels) <= cap:
        return f"{ot} needs between 2 and {cap} labels (got {len(labels)})"
    if any(not lb for lb in labels) or len(set(labels)) != len(labels):
        return "label names must be non-empty and unique"
    fail = config.get("fail_options") or []
    if not isinstance(fail, list) or any(str(f) not in labels for f in fail):
        return f"fail_options must be a subset of the labels: {', '.join(labels)}"
    if ot == MULTICLASS and len(set(map(str, fail))) == len(labels):
        return "fail_options cannot contain every label — every answer would FAIL"
    return None


def build_questions(config: dict) -> dict[str, dict[str, Any]]:
    """The Decisions-API `questions` map for this column: one question, or one per label for
    multi-label. Ids are ours (never sent to the model's inference, per TypeSafe)."""
    ot = _output_type(config)
    question = str(config["question"]).strip()
    crit = config.get("criteria") or {}
    if ot == BINARY:
        return {
            "q": {
                "type": "noul",
                "instructions": question,
                "criteria": {"true": str(crit["true"]), "false": str(crit["false"])},
            }
        }
    if ot == MULTICLASS:
        return {
            "q": {
                "type": "choice",
                "instructions": question,
                "criteria": {str(k): _desc(v) for k, v in crit.items()},
            }
        }
    out: dict[str, dict[str, Any]] = {}
    for i, (label, desc) in enumerate(crit.items()):
        meaning = desc if isinstance(desc, str) and desc.strip() else f"`{label}` applies"
        out[f"l{i}"] = {
            "type": "noul",
            "instructions": f"{question}\nDoes the label `{label}` apply?",
            "criteria": {"true": meaning, "false": f"`{label}` does not apply"},
        }
    return out


def request_text(state: str, questions: dict[str, dict]) -> str:
    """What the decision model reads against its budget, for the token estimate: TypeSafe's 32k
    covers the state plus the LONGEST single question (the rest share the state in parallel)."""
    longest = max(
        (json.dumps(q, ensure_ascii=False) for q in questions.values()), key=len, default=""
    )
    return state + longest


def answer_fields(config: dict, answers: dict[str, dict]) -> dict[str, Any]:
    """Decisions-API answers (keyed like `build_questions`) → the `EvalResult` fields
    `(verdict, data_type, value, string_value, comment)`. Shared by the decision call and the LLM
    fallback."""
    ot = _output_type(config)
    fail = {str(f) for f in (config.get("fail_options") or [])}
    thr = float(config.get("threshold", DEFAULT_THRESHOLD))
    if ot == BINARY:
        p = _unit((answers.get("q") or {}).get("noul"))
        want_yes = str(config.get("pass_when") or "yes") == "yes"
        ok = (p >= thr) == want_yes
        return {
            "verdict": "PASS" if ok else "FAIL",
            "data_type": "NUMERIC",
            "value": p,
            "string_value": "",
            "comment": f"P(yes)={p:.2f} · pass when {'yes' if want_yes else 'no'} (threshold {thr:g})",
        }
    # Comments are what failure clustering groups on (`domain/failure/signature.py` masks the
    # numbers, then compares text), so they lead with the DECISION in a stable order — never with
    # a probability ranking that reshuffles from item to item. The full distribution is in the
    # eval recording, one click away in the cell.
    if ot == MULTICLASS:
        a = answers.get("q") or {}
        label = str(a.get("choice") or "")
        conf = a.get("confidence")
        comment = label + (
            f" (confidence {float(conf):.2f})" if isinstance(conf, (int, float)) else ""
        )
        return {
            "verdict": ("FAIL" if label in fail else "PASS") if fail else "",
            "data_type": "CATEGORICAL",
            "value": _unit(conf) if isinstance(conf, (int, float)) else None,
            "string_value": label,
            "comment": comment,
        }
    labels = _labels(config)
    probs = [(lb, _unit((answers.get(f"l{i}") or {}).get("noul"))) for i, lb in enumerate(labels)]
    fired = [(lb, p) for lb, p in probs if p >= thr]  # label order, not probability order
    comment = (
        "applies: " + ", ".join(f"{lb} {p:.2f}" for lb, p in fired) if fired else "no label applies"
    ) + f" (threshold {thr:g})"
    return {
        "verdict": ("FAIL" if fail & {lb for lb, _ in fired} else "PASS") if fail else "",
        "data_type": "CATEGORICAL",
        "value": float(len(fired)),
        "string_value": ", ".join(lb for lb, _ in fired) or "none",
        "comment": comment,
    }


def _unit(v: Any) -> float:
    try:
        return min(max(float(v), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


# ── LLM fallback: the same questions, the same answer space ──────────────────


def fallback_system_prompt(config: dict) -> str:
    """The system prompt an LLM fallback grades under: the column's question and criteria, as a
    closed-answer classification — no rubric of our own, so both models answer the same thing."""
    ot = _output_type(config)
    crit = config.get("criteria") or {}
    lines = [
        "You answer one classification question about the STATE in the user message.",
        "Read the question literally and answer only from the state.",
        "",
        f"Question: {str(config['question']).strip()}",
    ]
    if ot == BINARY:
        lines += [
            f"- yes means: {crit['true']}",
            f"- no means: {crit['false']}",
            "Return probability_yes: your calibrated probability (0..1) that the answer is yes.",
        ]
        return "\n".join(lines)
    lines.append("Labels:")
    lines += [
        f"- {k}" + (f": {v if isinstance(v, str) else json.dumps(v)}" if _desc(v) else "")
        for k, v in crit.items()
    ]
    if ot == MULTICLASS:
        lines.append("Return the single best label and your confidence (0..1).")
    else:
        lines.append(
            "Judge each label on its own (any number may apply, including none) and return, for "
            "every label, your calibrated probability (0..1) that it applies."
        )
    return "\n".join(lines)


def fallback_schema(config: dict) -> type[BaseModel]:
    ot = _output_type(config)
    if ot == BINARY:
        return create_model(
            "BinaryAnswer",
            probability_yes=(float, Field(ge=0, le=1, description="probability the answer is yes")),
        )
    label_type = Literal[tuple(_labels(config))]
    if ot == MULTICLASS:
        return create_model(
            "MulticlassAnswer",
            label=(label_type, Field(description="the single best label")),
            confidence=(float, Field(ge=0, le=1)),
        )
    # One probability per label — the same answer Jev gives (a `noul` each), so the column's
    # threshold means the same thing whichever model graded the item. Field names are positional
    # (`l0`, `l1`, …; label names need not be identifiers); the description carries the label.
    crit = config.get("criteria") or {}
    fields: dict[str, Any] = {}
    for i, lb in enumerate(_labels(config)):
        desc = crit.get(lb)
        extra = f" ({desc})" if isinstance(desc, str) and desc.strip() else ""
        fields[f"l{i}"] = (
            float,
            Field(ge=0, le=1, description=f"probability that label `{lb}` applies{extra}"),
        )
    return create_model("MultilabelAnswer", **fields)


def fallback_answers(config: dict, reply: BaseModel) -> dict[str, dict]:
    """An LLM fallback's structured reply, reshaped as the Decisions API would have answered."""
    ot = _output_type(config)
    data = reply.model_dump()
    if ot == BINARY:
        return {"q": {"type": "noul", "noul": data["probability_yes"]}}
    if ot == MULTICLASS:
        return {
            "q": {
                "type": "choice",
                "choice": data["label"],
                "confidence": data["confidence"],
                "probabilities": {data["label"]: data["confidence"]},
            }
        }
    return {f"l{i}": {"type": "noul", "noul": data[f"l{i}"]} for i in range(len(_labels(config)))}
