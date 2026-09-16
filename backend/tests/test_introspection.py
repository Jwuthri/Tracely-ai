"""Recording Tracely's own work as a trace (`domain/introspection.py`).

The load-bearing property here is NOT the pretty tree — it is that a recording can never be
evaluated. An eval run that gets evaluated records another eval run, which gets evaluated: the
worker fills with work that generates more work, and the first symptom is a bill.
"""

from __future__ import annotations

import base64
import json

import pytest

from tracely.domain import introspection
from tracely.domain.introspection import Recording


def _spans(payloads):
    """Every span the recording emitted, across its per-level traces (usually one)."""
    return [
        span
        for body in payloads.values()
        for span in body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    ]


def _attrs(span):
    out = {}
    for a in span["attributes"]:
        v = a["value"]
        out[a["key"]] = v.get("stringValue", v.get("boolValue", v.get("intValue")))
    return out


def _rec(**over):
    kw = dict(kind=introspection.EVAL, subject_id="tr1", name="eval · 2", project_id="p1")
    kw.update(over)
    return Recording(**kw)


# ── the loop guard ────────────────────────────────────────────────────────────


def test_every_span_is_marked_internal():
    """The mark is the guard: ingest and EvaluationService both refuse a trace that has it, so a
    span that slips through unmarked is a span that gets evaluated — and evaluating an evaluation
    never terminates."""
    rec = _rec()
    rec.label = "correctness"
    rec.add("openai/gpt-5", input="grade this", output="PASS")

    spans = _spans(introspection.payload(rec))

    assert spans, "a recording with steps must produce spans"
    for span in spans:
        assert _attrs(span)["tracely.internal.kind"] == "eval"


def test_nothing_recorded_means_no_trace():
    """Nothing declared and nothing done — emitting an empty trace for that would bury the real
    recordings."""
    assert introspection.payload(_rec()) == {}


# ── legibility ────────────────────────────────────────────────────────────────


def test_a_group_that_made_no_llm_call_still_appears():
    """A structural evaluator calls no model. If only groups with steps were emitted, the
    recording would show the judges alone and read as 'these are the ones that ran'."""
    rec = _rec()
    rec.label = "tracely.run.outcome"
    rec.describe(input="structural · AGENT_RUN", output="PASS")

    names = [s["name"] for s in _spans(introspection.payload(rec))]
    assert "tracely.run.outcome" in names


def test_the_verdict_lives_on_the_evaluator_span():
    """One row per evaluator, not two. A separate child 'verdict' event doubled the row count and
    told the reader nothing its parent couldn't."""
    rec = _rec()
    rec.label = "tracely.run.quality"
    rec.describe(input="llm_judge · AGENT_RUN · basic", output="FAIL — invented a refund policy")

    spans = _spans(introspection.payload(rec))
    assert not any(s["name"] == "verdict" for s in spans)
    a = _attrs(next(s for s in spans if s["name"] == "tracely.run.quality"))
    assert a["tracely.input"] == "llm_judge · AGENT_RUN · basic"
    assert "invented a refund policy" in a["tracely.output"]


def test_the_root_says_what_was_graded_not_just_its_id():
    """The id alone as a title was the complaint: `0000…1f1ffee` tells a reader nothing."""
    rec = _rec(subject_label="grading trace tr1\n\nWhere is my order?")
    rec.label = "x"
    rec.describe(output="PASS")

    root = next(s for s in _spans(introspection.payload(rec)) if "parentSpanId" not in s)
    assert "Where is my order?" in _attrs(root)["tracely.input"]


def test_the_root_summarises_every_verdict():
    """So a collapsed recording still answers "what did this eval decide?" — as an object, which
    the table pretty-prints, rather than the run-on sentence it used to concatenate."""
    rec = _rec()
    rec.label = "a"
    rec.describe(output="PASS")
    rec.label = "b"
    rec.describe(output="FAIL — rude")

    root = next(s for s in _spans(introspection.payload(rec)) if "parentSpanId" not in s)
    assert json.loads(_attrs(root)["tracely.output"]) == {"a": "PASS", "b": "FAIL — rude"}


def test_a_single_verdict_is_the_roots_output_verbatim():
    """One column has nothing to disambiguate, so its payload is not wrapped in its own name —
    a `json` judge's schema stays an openable object on the collapsed row."""
    rec = _rec()
    rec.label = "tracely.run.correction"
    rec.describe(output='{"verdict": "FAIL", "reason": "user had to repeat themselves"}')

    root = next(s for s in _spans(introspection.payload(rec)) if "parentSpanId" not in s)
    assert json.loads(_attrs(root)["tracely.output"])["verdict"] == "FAIL"


def test_groups_keep_declaration_order():
    """Evaluators run in dependency order; the recording must not reshuffle them."""
    rec = _rec()
    for name in ("first", "second", "third"):
        rec.label = name
        rec.describe(output="PASS")

    spans = _spans(introspection.payload(rec))
    root = next(s for s in spans if "parentSpanId" not in s)
    groups = [s["name"] for s in spans if s.get("parentSpanId") == root["spanId"]]
    assert groups == ["first", "second", "third"]


# ── one trace per level ───────────────────────────────────────────────────────


def _level(rec, group, level, **kw):
    rec.label = group
    rec.describe(meta={"level": level}, **kw)


def test_each_level_gets_its_own_trace():
    """The traces table groups by conversation id, so a run that mixes a message column with a
    step column produces a row with no level of its own — a verdict you can't attribute."""
    rec = _rec(name="eval · {level} · {n} column(s)", conversation_id="eval:t1")
    _level(rec, "tracely.run.quality", "msg", output="FAIL")
    _level(rec, "tracely.run.reask", "msg", output="PASS")
    _level(rec, "tracely.tool.success", "step", output="PASS")

    bodies = introspection.payload(rec)
    assert len(bodies) == 2
    roots = {}
    for body in bodies.values():
        root = next(s for s in _spans({0: body}) if "parentSpanId" not in s)
        roots[root["name"]] = _attrs(root)["tracely.conversation.id"]
    # the title counts the columns THIS trace ran, and the row it lands in is that level's row
    assert roots == {
        "eval · msg · 2 column(s)": "eval:t1:msg",
        "eval · step · 1 column(s)": "eval:t1:step",
    }


def test_a_replaceable_recording_keeps_its_trace_id():
    """The conversation pass re-grades a thread every time it grows. Without a stable id that is
    six near-identical runs in the row; with one, the emitter can drop the old spans and the row
    shows the current verdict."""
    def graded(verdict):
        rec = _rec(subject_id="thread-1", stable=True)
        _level(rec, "tracely.conv.trajectory", "conv", output=verdict)
        return introspection.payload(rec)

    assert list(graded("PASS")) == list(graded("FAIL"))

    # an ordinary (per-message) recording still gets a fresh id every time — nothing to replace
    def once():
        rec = _rec(subject_id="tr1")
        _level(rec, "tracely.run.quality", "msg", output="PASS")
        return introspection.payload(rec)

    assert list(once()) != list(once())


# ── shape ─────────────────────────────────────────────────────────────────────


def test_ids_are_base64_so_the_otlp_parser_can_read_them():
    """Same trap as emulated turns: protobuf's json_format decodes `bytes` as base64, and hex does
    not raise — it silently yields a 24-byte id nothing can look up."""
    rec = _rec()
    rec.add("call", input="x", output="y")

    for span in _spans(introspection.payload(rec)):
        assert len(base64.b64decode(span["traceId"], validate=True)) == 16
        assert len(base64.b64decode(span["spanId"], validate=True)) == 8


def test_steps_nest_under_their_group():
    """Grouping is what makes the recording readable: 'which evaluator asked this?' has to be
    answerable by looking at the tree, not by reading prompts."""
    rec = _rec()
    rec.label = "correctness"
    rec.add("openai/gpt-5", input="a", output="b")
    rec.label = "tone"
    rec.add("openai/gpt-5", input="c", output="d")

    spans = _spans(introspection.payload(rec))
    by_id = {s["spanId"]: s for s in spans}
    root = next(s for s in spans if "parentSpanId" not in s)
    groups = {s["name"]: s for s in spans if s.get("parentSpanId") == root["spanId"]}

    assert set(groups) == {"correctness", "tone"}
    leaves = [s for s in spans if s.get("parentSpanId") in {g["spanId"] for g in groups.values()}]
    assert len(leaves) == 2
    assert by_id[leaves[0]["parentSpanId"]]["name"] == "correctness"


def test_ungrouped_steps_hang_off_the_root():
    rec = _rec()
    rec.add("solo", input="a", output="b")

    spans = _spans(introspection.payload(rec))
    root = next(s for s in spans if "parentSpanId" not in s)
    assert [s["name"] for s in spans if s.get("parentSpanId") == root["spanId"]] == ["solo"]


def test_the_prompt_and_the_reply_are_both_kept():
    """The whole point: 'why did the judge say that' is answered by reading what it was asked."""
    rec = _rec()
    rec.add("openai/gpt-5", input="[system]\nbe strict\n\n[user]\ngrade", output="FAIL: rude")

    leaf = next(s for s in _spans(introspection.payload(rec)) if s["name"] == "openai/gpt-5")
    a = _attrs(leaf)
    assert "be strict" in a["tracely.input"] and a["tracely.output"] == "FAIL: rude"


def test_a_failed_step_carries_the_error():
    rec = _rec()
    rec.label = "correctness"
    rec.add("openai/gpt-5", input="a", error="rate limited")

    spans = _spans(introspection.payload(rec))
    leaf = next(s for s in spans if s["name"] == "openai/gpt-5")
    group = next(s for s in spans if s["name"] == "correctness")
    assert leaf["status"]["code"] == 2 and "rate limited" in leaf["status"]["message"]
    # The group surfaces it too, so a collapsed tree still shows something went wrong.
    assert group["status"]["code"] == 2


def test_subject_travels_on_every_span():
    """`subject_id` is how the UI finds 'the eval for THIS trace'; a span without it is orphaned."""
    rec = _rec(subject_id="conv-9", kind=introspection.SIM)
    rec.add("POST /chat", input="hi", output="hello")

    for span in _spans(introspection.payload(rec)):
        assert _attrs(span)["tracely.internal.subject_id"] == "conv-9"


def test_token_usage_is_reported_as_gen_ai_attributes():
    """So judge spend shows up in the recording with the same columns as any other LLM span."""
    rec = _rec()
    rec.add("openai/gpt-5", input="a", output="b",
            tokens={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128})

    leaf = next(s for s in _spans(introspection.payload(rec)) if s["name"] == "openai/gpt-5")
    assert _attrs(leaf)["gen_ai.usage.input_tokens"] == "120"


# ── the context manager ───────────────────────────────────────────────────────


@pytest.fixture
def recording_on(monkeypatch):
    """Recording is hard-OFF for the suite (conftest) so no test writes an internal trace into a
    developer's own ClickHouse. These tests exercise the mechanism itself, so they turn it back
    on — with `_emit` stubbed, so still nothing leaves the process."""
    from tracely.services import introspection_service as svc

    monkeypatch.setattr(svc.settings, "introspection_enabled", True)


def test_recording_is_off_when_disabled(monkeypatch):
    from tracely.services import introspection_service as svc

    monkeypatch.setattr(svc.settings, "introspection_enabled", False)
    with svc.record(introspection.EVAL, "tr1", "eval", project_id="p1") as rec:
        assert rec is None
        assert introspection.active() is None


def test_recordings_do_not_nest(monkeypatch, recording_on):
    """Grading inside a scenario run must keep filing into the scenario's recording. Two open
    recordings would split one story across two traces with nothing linking them."""
    emitted: list = []
    from tracely.services import introspection_service as svc

    monkeypatch.setattr(svc, "_emit", emitted.append)
    with svc.record(introspection.SIM, "conv1", "outer", project_id="p1") as outer:
        with svc.record(introspection.EVAL, "tr1", "inner", project_id="p1") as inner:
            assert inner is None
            assert introspection.active() is outer

    assert [r.name for r in emitted] == ["outer"]


def test_the_active_recording_is_cleared_even_when_the_work_raises(monkeypatch, recording_on):
    """A leaked contextvar would attach the next unrelated eval to this trace."""
    from tracely.services import introspection_service as svc

    monkeypatch.setattr(svc, "_emit", lambda _rec: None)
    with pytest.raises(ValueError):
        with svc.record(introspection.EVAL, "tr1", "eval", project_id="p1"):
            raise ValueError("boom")
    assert introspection.active() is None


def test_a_broken_emit_never_breaks_the_work(monkeypatch, recording_on):
    """Recording is observability. A judge that graded correctly must not report failure because
    its recording could not be saved."""
    from tracely.services import introspection_service as svc

    monkeypatch.setattr(svc.blobstore, "put_blob", lambda *a, **k: (_ for _ in ()).throw(OSError("s3 down")))
    with svc.record(introspection.EVAL, "tr1", "eval", project_id="p1") as rec:
        rec.add("openai/gpt-5", input="a", output="b")
    # no exception escaped


# ── the loop guard, at both boundaries ────────────────────────────────────────


def test_ingest_never_schedules_evaluation_for_a_recording(monkeypatch):
    """Layer 1. Without this, ingesting an eval recording queues an evaluation of it, which
    records another recording — the worker generates its own work until someone notices the bill."""
    from tracely.workers import tasks

    scheduled: list[str] = []
    monkeypatch.setattr(
        tasks.IngestionService, "process_blob",
        lambda self, p, k, c: {
            "events": 4, "trace_ids": ["real-1", "rec-1"], "internal_trace_ids": ["rec-1"],
        },
    )
    monkeypatch.setattr(tasks.eval_debounce, "bump", lambda p, t: 1)
    monkeypatch.setattr(
        tasks.evaluate_run_task, "apply_async",
        lambda args, **kw: scheduled.append(args[1]),
    )

    tasks.ingest_otlp_blob.run("p1", "some/key.json", "application/json")

    assert scheduled == ["real-1"]


def test_a_verdict_is_recorded_as_an_object_for_llm_and_structural_alike():
    """A structural check has no nested prompt/reply to open, so its row IS its verdict — which
    means the verdict has to be as readable as a judge's. Same shape for both; a `json` judge's own
    schema is re-parsed rather than embedded as an escaped string."""
    import json as _json

    from tracely.domain.evaluation.results import EvalResult
    from tracely.services.evaluation_service import _results_json, _spec_json

    structural = _json.loads(_results_json([EvalResult("x", "AGENT_RUN", "PASS", value=288.0)]))
    assert structural == {"verdict": "PASS", "value": 288.0}

    judged = _json.loads(_results_json([
        EvalResult("y", "AGENT_RUN", "FAIL", value=0.2, comment="rude",
                   string_value='{"severity": "severe"}'),
    ]))
    assert judged["output"] == {"severity": "severe"}  # nested, not a quoted string

    # Several graded steps stay an array so each one keeps its span.
    many = _json.loads(_results_json([
        EvalResult("z", "TOOL", "PASS", target_span_id="s1"),
        EvalResult("z", "TOOL", "FAIL", target_span_id="s2"),
    ]))
    assert [r["span_id"] for r in many] == ["s1", "s2"]

    assert _json.loads(_spec_json(
        {"score_name": "z", "kind": "structural", "level": "TOOL", "config": {"check": "tool_success"}}
    )) == {"evaluator": "z", "kind": "structural", "level": "step", "mode": "batch",
           "check": "tool_success"}


def test_evaluation_refuses_a_trace_that_is_itself_a_recording(monkeypatch):
    """Layer 2. Ingest is not the only caller — a monitor, a manual re-run or a backfill reaches
    `evaluate_trace` directly, and each is its own way into the same loop."""
    from tracely.services.evaluation_service import EvaluationService

    class _Reader:
        client = None

        def read_spans(self, project_id, trace_id):
            return [{"span_id": "s1", "parent_span_id": "", "internal_kind": "eval"}]

    svc = EvaluationService(trace_reader=_Reader(), score_writer=object())
    assert svc.evaluate_trace("p1", "rec-1") == {"scores": 0, "skipped": "internal"}


def test_the_root_is_labelled_by_the_columns_it_ran():
    """The run root belongs to no single column, so without this its Agent cell is the only blank
    one in the recording — or worse, the `default` fallback agent, which produced none of it."""
    rec = _rec()
    for name in ("tracely.run.outcome", "tracely.run.quality", "tracely.tool.success"):
        rec.label = name
        rec.describe(output="PASS")

    root = next(s for s in _spans(introspection.payload(rec)) if "parentSpanId" not in s)
    a = _attrs(root)
    assert a["tracely.metadata.evaluator"] == "tracely.run.outcome +2"
    # the root's name already IS the run — repeating it as step_name duplicates the Name column
    assert "tracely.step.name" not in a


def test_a_single_column_run_names_it_outright():
    rec = _rec()
    rec.label = "tracely.run.quality"
    rec.describe(output="PASS")

    root = next(s for s in _spans(introspection.payload(rec)) if "parentSpanId" not in s)
    assert _attrs(root)["tracely.metadata.evaluator"] == "tracely.run.quality"


# ── the conversation pass runs once, not once per message ─────────────────────


def test_ingest_defers_the_conversation_pass_instead_of_running_it_per_message(monkeypatch):
    """It used to run inline in `evaluate_trace`, so a 60-turn thread re-graded the whole
    conversation 60 times — the same judge, the same transcript, 60× the spend."""
    from tracely.workers import tasks

    calls: dict = {}
    def fake_trace(self, p, t, **kw):
        calls["skip"] = kw.get("skip_conversation")
        calls["mode"] = kw.get("execution_mode")
        return {"scores": 1, "thread_id": "th-1", "needs_thread_pass": True}

    def fake_bump(p, key):
        calls["key"] = key
        return 7

    monkeypatch.setattr(tasks.EvaluationService, "evaluate_trace", fake_trace)
    monkeypatch.setattr(tasks.eval_debounce, "is_latest", lambda p, t, g: True)
    monkeypatch.setattr(tasks.eval_debounce, "bump", fake_bump)
    monkeypatch.setattr(
        tasks.evaluate_conversation_task, "apply_async",
        lambda args, **kw: calls.setdefault("scheduled", args),
    )

    tasks.evaluate_run_task.run("p1", "tr-1")

    assert calls["skip"] is True                      # not graded inline
    assert calls["mode"] == "batch"                   # sequential columns wait for the thread pass
    assert calls["scheduled"] == ("p1", "th-1", 7)    # scheduled once, for the thread
    assert calls["key"] == "conv:th-1"                # namespaced off the per-trace counter


def test_only_the_last_scheduled_conversation_pass_runs(monkeypatch):
    """Every message schedules one; all but the last must fall away, or the debounce buys nothing."""
    from tracely.workers import tasks

    ran: list[str] = []
    monkeypatch.setattr(tasks.eval_debounce, "is_latest", lambda p, t, g: g == 3)
    monkeypatch.setattr(
        tasks.EvaluationService, "evaluate_thread",
        lambda self, p, th, **kw: ran.append((th, kw.get("execution_mode"))) or {"scores": 1},
    )

    assert tasks.evaluate_conversation_task.run("p1", "th-1", 1) == {
        "skipped": "superseded", "thread_id": "th-1"
    }
    tasks.evaluate_conversation_task.run("p1", "th-1", 3)
    assert ran == [("th-1", "sequential")]


# ── the deferred thread pass is only scheduled when there is one to run ───────


def _run_ingest_eval(monkeypatch, evaluate_result: dict) -> dict:
    from tracely.workers import tasks

    calls: dict = {}
    monkeypatch.setattr(
        tasks.EvaluationService, "evaluate_trace",
        lambda self, p, t, **kw: evaluate_result,
    )
    monkeypatch.setattr(tasks.eval_debounce, "is_latest", lambda p, t, g: True)
    monkeypatch.setattr(tasks.eval_debounce, "bump", lambda p, key: 7)
    monkeypatch.setattr(
        tasks.evaluate_conversation_task, "apply_async",
        lambda args, **kw: calls.setdefault("scheduled", args),
    )
    tasks.evaluate_run_task.run("p1", "tr-1")
    return calls


def test_no_thread_pass_is_scheduled_when_no_column_needs_one(monkeypatch):
    """A project with only batch message columns has nothing for the thread pass to do, so the
    task existed only to find that out — a queue hop and a DB round-trip on every message."""
    calls = _run_ingest_eval(
        monkeypatch, {"scores": 1, "thread_id": "th-1", "needs_thread_pass": False}
    )
    assert "scheduled" not in calls


def test_a_conversation_or_sequential_column_still_schedules_it(monkeypatch):
    calls = _run_ingest_eval(
        monkeypatch, {"scores": 1, "thread_id": "th-1", "needs_thread_pass": True}
    )
    assert calls["scheduled"] == ("p1", "th-1", 7)


def test_chained_grade_records_the_earlier_steps():
    """Sequential sends only the new item over the wire (the checkpointer holds the rest), so
    without this step 3's recorded INPUT is byte-identical to a batch grade's."""
    from tracely.domain import introspection
    from tracely.infrastructure.llm import provider

    rec = _rec()
    token = introspection._active.set(rec)
    try:
        rec.context = "Step 1 of 2 — TOOL `faq`\n\n[user]\n"
        with provider._recorded("Step 2 of 2 — TOOL `echo`", "rubric", "m") as sink:
            sink.append(("{}", {}))
    finally:
        introspection._active.reset(token)

    assert rec.steps[0].input == (
        "[system]\nrubric\n\n[user]\nStep 1 of 2 — TOOL `faq`\n\n[user]\nStep 2 of 2 — TOOL `echo`"
    )
    assert rec.context == ""  # consumed: it describes one call



def test_stable_trace_id_matches_the_emitted_trace_key():
    """The UI re-derives this id to find the judge prompt behind a score — if it drifts from the
    id the emitter uses, the panel silently shows no prompt."""
    rec = introspection.Recording(
        kind=introspection.EVAL, subject_id="trace-1", name="eval · {level}",
        project_id="p1", stable=True,
    )
    rec.label = "tone"
    rec.describe(input="{}", output="PASS", meta={"level": "msg"})
    keys = list(introspection.payload(rec))
    assert keys == [introspection.stable_trace_id("p1", introspection.EVAL, "trace-1", "msg")]
