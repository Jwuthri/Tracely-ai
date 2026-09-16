"""Recording what Tracely itself did, as a trace.

When an evaluator grades a run or a scenario drives a conversation, the interesting part —
*which prompt went to which model, and what came back* — used to exist only in worker logs. This
module captures it into an ordinary OTLP payload, so the trace viewer that already renders agent
runs renders Tracely's own runs too.

Three kinds, kept apart by `internal_kind`:

- `eval` — one recording per evaluation of a trace, a group span per evaluator.
- `sim`  — one recording per scenario phase (driving, then grading).
- `assistant` — one recording per turn of the in-app chat agent: the models it called, and every
  tool it ran against the product with the caller's own credentials. The one place you can see
  *why* the assistant did what it did, in the same viewer as everything else.

Pure: dataclasses plus payload building. `services/introspection_service.py` owns the contextvar
lifecycle and the emit; `infrastructure/llm/provider.py` appends a step per LLM call.

**Recordings are never evaluated.** `internal_kind` is non-empty on every span here, and both the
ingest hop and `EvaluationService` refuse to grade such a trace — otherwise grading an eval run
would record another eval run, without end.
"""

from __future__ import annotations

import contextvars
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from tracely.domain import otlp_payload as otlp

EVAL = "eval"
SIM = "sim"
ASSISTANT = "assistant"

# The recording in progress on this task, if any. Lives here rather than in the service so
# `infrastructure/llm/provider.py` can append to it without importing upwards through the layers.
_active: contextvars.ContextVar["Recording | None"] = contextvars.ContextVar(
    "tracely_recording", default=None
)


def active() -> "Recording | None":
    return _active.get()

# Observation types, chosen so the existing renderers do the right thing: GENERATION shows the
# model + token counts, TOOL shows a call, CHAIN groups.
_GENERATION = "GENERATION"
_CHAIN = "CHAIN"


@dataclass
class Step:
    """One thing that happened: an LLM call, an HTTP request to the customer's endpoint, a
    decision. `group` is the label it belongs under (an evaluator name, a turn) — steps with the
    same group share a parent span."""

    name: str
    input: str = ""
    output: str = ""
    group: str = ""
    obs_type: str = _GENERATION
    model: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""
    start_ns: int = 0
    end_ns: int = 0


@dataclass
class Group:
    """One unit of work inside a recording — an evaluator, a conversation turn. Carries its own
    input/output so it is legible on its own: reading `llm_judge · AGENT_RUN` → `FAIL — invented a
    refund policy` should not require expanding it.

    `meta` (evaluator / level / kind) is stamped on the group AND on every step under it, so the
    table's metadata column answers "which column, at what level?" on any row — not just the one
    you happened to expand.
    """

    name: str
    input: str = ""
    output: str = ""
    error: str = ""
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Recording:
    """An in-progress recording. `label` is the group new steps are filed under — callers set it
    as they move through their work (per evaluator, per turn) so a flat sequence of provider calls
    becomes a readable tree.

    Setting `label` *declares* the group, so a unit of work that makes no LLM call at all (a
    structural evaluator) still appears. Otherwise the recording would silently show only the
    evaluators that happened to call a model, which reads as "these are the ones that ran".
    """

    kind: str
    subject_id: str
    # `{level}` / `{n}` are substituted per emitted trace — a recording that graded 5 message
    # columns and 1 step column is two traces, and one title cannot describe both.
    name: str
    project_id: str = ""
    agent_slug: str = ""
    env: str = "prod"
    # What the run is ABOUT, in words. The subject id alone is unreadable as a title.
    subject_label: str = ""
    # Groups every recording about the same conversation into ONE row of the traces table, so the
    # eval hierarchy lands on the table's own hierarchy: conversation → one run per turn (plus the
    # conversation-level run) → one step per evaluator column. Namespaced (`eval:`/`sim:`) so it
    # can never merge with the real conversation it is about. `payload` suffixes the level, so a
    # row is one level throughout.
    conversation_id: str = ""
    turn_index: int = 0
    # This recording REPLACES the last one about the same subject instead of stacking next to it:
    # its trace id is derived from (project, kind, subject, level) rather than random, and the
    # emitter drops the previous spans first. For work that legitimately re-runs over the same
    # subject — the conversation-level eval pass re-grades a thread every time it grows, and 6
    # near-identical runs in one row is noise, not history.
    stable: bool = False
    steps: list[Step] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    start_ns: int = field(default_factory=time.time_ns)
    _label: str = ""
    # Names the NEXT captured step — set by a step-level judge before each span it grades, so N
    # calls read as the spans they judged instead of N identical rows named after the model.
    target: str = ""
    # The turns already on this call's conversation, consumed by the provider into the NEXT
    # captured step's input. A chained (sequential) grade sends ONLY the new item over the wire —
    # the checkpointer holds the earlier ones — so recording the wire prompt alone made step 3's
    # INPUT byte-identical to a batch grade's: the trace showed the step being judged with no sight
    # of steps 1–2, and sequential looked broken when it was working.
    context: str = ""

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, name: str) -> None:
        self._label = name
        if name and not any(g.name == name for g in self.groups):
            self.groups.append(Group(name))

    def describe(
        self, *, input: str = "", output: str = "", error: str = "", meta: dict | None = None
    ) -> None:
        """Set the current group's own input/output — what it was asked to do and what it decided.
        This is what replaced a separate 'verdict' child span per evaluator: one row, not two."""
        group = next((g for g in self.groups if g.name == self._label), None)
        if group is None:
            return
        if input:
            group.input = input
        if output:
            group.output = output
        if error:
            group.error = error
        if meta:
            group.meta.update({str(k): str(v) for k, v in meta.items() if v})

    def add(
        self,
        name: str,
        *,
        input: str = "",
        output: str = "",
        obs_type: str = _GENERATION,
        model: str = "",
        tokens: dict[str, int] | None = None,
        error: str = "",
        start_ns: int = 0,
    ) -> None:
        now = time.time_ns()
        # A target set by the caller wins over the generic name (the model id), and is consumed —
        # it describes one call, not every call that follows it.
        label, self.target = (self.target or name), ""
        self.steps.append(Step(
            name=label, input=input, output=output, group=self._label, obs_type=obs_type,
            model=model, tokens=tokens or {}, error=error,
            start_ns=start_ns or now, end_ns=now,
        ))


def json_value(text: str) -> Any:
    """A recorded field back as data when it is data, verbatim when it is prose — so nesting one
    recorded payload inside another gives an object rather than a wall of escaped quotes."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _root_output(groups: list["Group"], steps: list["Step"]) -> str:
    """Every group's verdict as ONE value, for the collapsed row.

    JSON rather than `a: PASS · b: FAIL — rude`: the table pretty-prints and highlights anything
    that parses, so the same summary that used to be a run-on sentence becomes an object you can
    open. A single group contributes its payload verbatim — with nothing to disambiguate it,
    wrapping it in its own name adds a layer that says nothing.
    """
    graded = [g for g in groups if g.output]
    if not graded:
        return f"{len(steps)} step(s)"
    if len(graded) == 1:
        return graded[0].output
    return json.dumps(
        {g.name: json_value(g.output) for g in graded}, indent=2, ensure_ascii=False
    )


def _usage(tokens: dict[str, int]) -> list[dict]:
    """Token counts as the `gen_ai.usage.*` attributes the span mapper already reads."""
    out = []
    for key, attr_name in (
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("total_tokens", "gen_ai.usage.total_tokens"),
    ):
        if tokens.get(key):
            out.append(otlp.attr(attr_name, int(tokens[key])))
    return out


def payload(rec: Recording) -> dict[str, dict]:
    """The recording as ExportTraceServiceRequests, **one per level**, keyed by trace id (hex).

    Shape of each: a root span, one CHAIN per group, and each step underneath its group. Every
    span carries `tracely.internal.kind`, which is what keeps these traces out of the lists and
    out of the evaluator's reach.

    Split by `Group.meta["level"]` because the traces table groups by conversation id, and one row
    that mixes a message column with a step column has no level of its own — you read a verdict
    without knowing what it graded. Groups with no level (a scenario run) share one bucket, so
    that path emits exactly one trace as before.
    """
    if not rec.steps and not rec.groups:
        return {}

    # level → (its groups, in declaration order; the steps filed under them). A step whose group
    # was never declared keeps the old behavior — it lands in the unlevelled bucket, under root.
    buckets: dict[str, tuple[list[Group], list[Step]]] = {}
    level_of = {g.name: g.meta.get("level", "") for g in rec.groups}
    for group in rec.groups:
        buckets.setdefault(group.meta.get("level", ""), ([], []))[0].append(group)
    for step in rec.steps:
        buckets.setdefault(level_of.get(step.group, ""), ([], []))[1].append(step)

    out = {}
    for level, (groups, steps) in buckets.items():
        trace_id = _trace_id(rec, level)
        out[trace_id.hex()] = _one(rec, level, groups, steps, trace_id)
    return out


# Fixed namespace for stable trace ids — changing it orphans every recording already emitted.
_NAMESPACE = uuid.UUID("6e0f1b9a-6e5c-5a3f-9f2a-1c0d5b7e4a21")


def step_names(body: dict) -> list[str]:
    """The evaluator groups one emitted payload covers.

    A stable recording shares its trace id with every OTHER column graded at the same level, so
    replacing it wholesale is what made a per-column re-run erase the sibling columns' prompts.
    The emitter deletes only these groups instead — see `introspection_service._emit`.
    """
    names = set()
    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for a in sp.get("attributes", []):
                    if a.get("key") == "tracely.step.name":
                        names.add(a.get("value", {}).get("stringValue", ""))
    return sorted(n for n in names if n)


def stable_trace_id(project_id: str, kind: str, subject_id: str, level: str) -> str:
    """The hex trace id a stable recording lands on — so a reader (the UI asking "what prompt
    produced this score?") can address the recording without storing a pointer to it."""
    return uuid.uuid5(_NAMESPACE, f"{project_id}:{kind}:{subject_id}:{level}").hex


def _trace_id(rec: Recording, level: str) -> bytes:
    """Random, unless the recording replaces its predecessor — then the id IS the identity of
    (project, kind, subject, level), so re-recording lands on the same trace."""
    if not rec.stable:
        return uuid.uuid4().bytes
    return uuid.UUID(hex=stable_trace_id(rec.project_id, rec.kind, rec.subject_id, level)).bytes


def _one(
    rec: Recording, level: str, groups: list[Group], steps: list[Step], trace_id: bytes
) -> dict:
    name = rec.name.replace("{level}", level).replace("{n}", str(len(groups)))
    conversation_id = rec.conversation_id + (f":{level}" if rec.conversation_id and level else "")
    end_ns = max((s.end_ns for s in steps), default=time.time_ns())

    def base(group: Group | None = None, extra: list[dict] | None = None) -> list[dict]:
        meta = (group.meta if group else {})
        return [
            otlp.attr("tracely.internal.kind", rec.kind),
            otlp.attr("tracely.internal.subject_id", rec.subject_id),
            otlp.attr("tracely.env", rec.env),
            *([otlp.attr("tracely.agent.id", rec.agent_slug)] if rec.agent_slug else []),
            *([otlp.attr("tracely.conversation.id", conversation_id)]
              if conversation_id else []),
            *([otlp.attr("tracely.turn.index", rec.turn_index)] if rec.turn_index else []),
            # Which evaluator column, at what level — on EVERY span, so scanning the table answers
            # it without expanding anything. Skipped on the root, whose name already IS the run.
            *([otlp.attr("tracely.step.name", group.name)] if group and group is not root_group else []),
            *(otlp.attr(f"tracely.metadata.{k}", v) for k, v in meta.items()),
            *(extra or []),
        ]

    # The root belongs to no single column, so it names the ones it ran — the Agent column on that
    # row would otherwise be the only blank (or, worse, the fallback agent) in the recording.
    names = [g.name for g in groups]
    root_group = Group(
        name,
        meta={"evaluator": names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")}
        if names else {},
    )

    root_id = uuid.uuid4().bytes[:8]
    spans = [otlp.span(
        trace_id=trace_id, span_id=root_id, name=name,
        start_ns=rec.start_ns, end_ns=end_ns,
        attributes=base(root_group, extra=[
            otlp.attr("tracely.observation.type", _CHAIN),
            otlp.attr("tracely.is_app_root", True),
            otlp.attr("tracely.trace.name", name),
            otlp.attr("tracely.input", rec.subject_label or rec.subject_id),
            otlp.attr("tracely.output", _root_output(groups, steps)),
        ]),
    )]

    # One span per DECLARED group, in declaration order — including groups that recorded no step,
    # so a structural evaluator is visible as having run rather than silently absent.
    group_ids: dict[str, bytes] = {}
    for group in groups:
        members = [s for s in steps if s.group == group.name]
        gid = uuid.uuid4().bytes[:8]
        group_ids[group.name] = gid
        spans.append(otlp.span(
            trace_id=trace_id, span_id=gid, parent_span_id=root_id, name=group.name,
            start_ns=members[0].start_ns if members else rec.start_ns,
            end_ns=max((m.end_ns for m in members), default=rec.start_ns),
            attributes=base(group, [
                otlp.attr("tracely.observation.type", _CHAIN),
                otlp.attr("tracely.input", group.input),
                otlp.attr("tracely.output", group.output),
            ]),
            error=group.error or "; ".join(m.error for m in members if m.error),
        ))

    by_name = {g.name: g for g in groups}
    for step in steps:
        spans.append(otlp.span(
            trace_id=trace_id, span_id=uuid.uuid4().bytes[:8],
            parent_span_id=group_ids.get(step.group, root_id),
            name=step.name, start_ns=step.start_ns, end_ns=step.end_ns,
            attributes=base(by_name.get(step.group), [
                otlp.attr("tracely.observation.type", step.obs_type),
                otlp.attr("tracely.input", step.input),
                otlp.attr("tracely.output", step.output),
                *([otlp.attr("tracely.model", step.model)] if step.model else []),
                *_usage(step.tokens),
            ]),
            error=step.error,
        ))
    return otlp.request(spans, scope_name=f"tracely.internal.{rec.kind}")
