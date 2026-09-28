"""The on-demand SSE run endpoint + the thread-level orchestration of EvaluationService."""

from __future__ import annotations

import json

from tracely.services.evaluation_service import EvaluationService


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _owner_token(client) -> str:
    r = await client.post(
        "/auth/register", json={"email": "owner@x.test", "password": "hunter2-pw"}
    )
    return r.json()["token"]


_SPEC = {
    "id": "ev-1", "kind": "structural", "config": {"check": "run_outcome"},
    "score_name": "tracely.run.outcome", "level": "AGENT_RUN",
}


async def test_run_streams_per_score_frames(client, monkeypatch):
    tok = await _owner_token(client)

    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: [_SPEC]),
    )

    def fake_thread(self, project_id, thread_id, specs=None, on_result=None):
        on_result({
            "name": "tracely.run.outcome", "evaluation_level": "AGENT_RUN",
            "observation_id": None, "value": 1.0, "string_value": "", "verdict": "PASS",
            "comment": "", "data_type": "BOOLEAN", "trace_id": "tr-1", "session_id": thread_id,
        })
        return {"scores": 1, "failures": 0}

    monkeypatch.setattr(EvaluationService, "evaluate_thread", fake_thread)

    frames: list[str] = []
    async with client.stream(
        "POST", "/api/evaluations/run", headers=_bearer(tok), json={"thread_ids": ["th-1"]}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                frames.append(line[len("data: "):])

    assert frames[-1] == "[DONE]"
    events = [json.loads(f) for f in frames[:-1]]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert "result" in types and "target_done" in types
    result = next(e for e in events if e["type"] == "result")
    assert result["score"]["session_id"] == "th-1"
    assert result["score"]["verdict"] == "PASS"


async def test_run_trace_targets_skip_conversation(client, monkeypatch):
    tok = await _owner_token(client)
    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: [_SPEC]),
    )
    seen: dict = {}

    def fake_trace(self, project_id, trace_id, specs=None, on_result=None, skip_conversation=False):
        seen["skip_conversation"] = skip_conversation
        seen["trace_id"] = trace_id
        return {"scores": 0, "failures": 0}

    monkeypatch.setattr(EvaluationService, "evaluate_trace", fake_trace)
    async with client.stream(
        "POST", "/api/evaluations/run", headers=_bearer(tok), json={"trace_ids": ["tr-9"]}
    ) as r:
        async for _ in r.aiter_lines():
            pass
    assert seen == {"skip_conversation": True, "trace_id": "tr-9"}


async def test_run_requires_targets(client):
    tok = await _owner_token(client)
    r = await client.post("/api/evaluations/run", headers=_bearer(tok), json={})
    assert r.status_code == 400


async def test_run_with_no_matching_evaluators_is_400(client, monkeypatch):
    tok = await _owner_token(client)
    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: []),
    )
    r = await client.post(
        "/api/evaluations/run", headers=_bearer(tok), json={"thread_ids": ["th-1"]}
    )
    assert r.status_code == 400


# ── EvaluationService.evaluate_thread orchestration (no DB / no ClickHouse) ────────


class _FakeReader:
    def __init__(self, spans_by_trace: dict[str, list[dict]], order: list[str]) -> None:
        self.spans_by_trace, self.order = spans_by_trace, order

    def read_spans(self, project_id, trace_id):
        return self.spans_by_trace.get(trace_id, [])

    def read_thread_spans(self, project_id, thread_id):
        return [s for t in self.order for s in self.spans_by_trace[t]]

    def thread_trace_ids(self, project_id, thread_id):
        return list(self.order)


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list]] = []

    def write_eval_scores(self, project_id, trace_id, agent_run_id, results, thread_id=""):
        self.calls.append((trace_id, thread_id, list(results)))


def _ok_span(trace_id: str) -> dict:
    return {
        "span_id": f"{trace_id}-root", "parent_span_id": "", "type": "AGENT", "name": "run",
        "level": "DEFAULT", "status_message": "", "start_time": None, "end_time": None,
        "agent_id": "", "agent_run_id": trace_id, "input": "q", "output": "a",
        "tool_call_names": [], "trace_id": trace_id, "is_app_root": 1, "conversation_id": "th-1",
    }


def test_evaluate_thread_sequential_chains_across_turns():
    """A sequential metric's config gains __previous_result__ from the prior turn's result of
    the SAME metric; batch metrics never do; the first turn has no chain context."""
    from tracely.domain.evaluation.results import EvalResult

    class _FakeRegistry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def dispatch(self, kind, config, score_name, level, ctx):
            self.calls.append((score_name, dict(config)))
            n = sum(1 for name, _ in self.calls if name == score_name)
            return [EvalResult(score_name, "AGENT_RUN", "PASS", value=0.9, comment=f"turn {n}")]

    reader = _FakeReader({"t1": [_ok_span("t1")], "t2": [_ok_span("t2")]}, ["t1", "t2"])
    registry = _FakeRegistry()
    svc = EvaluationService(trace_reader=reader, score_writer=_FakeWriter(), registry=registry)  # type: ignore[arg-type]
    specs = [
        {"id": "seq", "kind": "llm_judge", "score_name": "custom.seq", "level": "AGENT_RUN",
         "config": {"prompt": "p", "execution_mode": "sequential"}},
        {"id": "batch", "kind": "llm_judge", "score_name": "custom.batch", "level": "AGENT_RUN",
         "config": {"prompt": "p"}},
    ]
    svc.evaluate_thread("p", "th-1", specs=specs)

    by_metric: dict[str, list[dict]] = {}
    for name, config in registry.calls:
        by_metric.setdefault(name, []).append(config)
    assert "__previous_result__" not in by_metric["custom.seq"][0]  # first turn: no context
    chained = by_metric["custom.seq"][1]["__previous_result__"]
    assert chained == {"value": 0.9, "verdict": "PASS", "reason": "turn 1"}
    assert all("__previous_result__" not in c for c in by_metric["custom.batch"])


def test_evaluate_thread_runs_turns_then_conversation():
    reader = _FakeReader({"t1": [_ok_span("t1")], "t2": [_ok_span("t2")]}, ["t1", "t2"])
    writer = _FakeWriter()
    svc = EvaluationService(trace_reader=reader, score_writer=writer)  # type: ignore[arg-type]
    specs = [
        {"id": "a", "kind": "structural", "config": {"check": "run_outcome"},
         "score_name": "tracely.run.outcome", "level": "AGENT_RUN"},
        {"id": "b", "kind": "structural", "config": {"check": "run_outcome"},
         "score_name": "custom.conv_outcome", "level": "CONVERSATION"},
    ]
    emitted: list[dict] = []
    out = svc.evaluate_thread("p", "th-1", specs=specs, on_result=emitted.append)

    assert out == {"scores": 3, "failures": 0}
    # two per-trace writes (thread stamped for session addressing) + one thread-scoped write
    assert [(c[0], c[1]) for c in writer.calls] == [("t1", "th-1"), ("t2", "th-1"), ("", "th-1")]
    conv_results = writer.calls[-1][2]
    assert [r.level for r in conv_results] == ["CONVERSATION"]
    conv_emit = next(e for e in emitted if e["evaluation_level"] == "CONVERSATION")
    assert conv_emit["trace_id"] is None and conv_emit["session_id"] == "th-1"
    run_emit = next(e for e in emitted if e["evaluation_level"] == "AGENT_RUN")
    assert run_emit["trace_id"] == "t1" and run_emit["session_id"] == "th-1"


def test_settled_thread_pass_runs_only_sequential_trace_columns():
    """The worker's settled-thread pass must not re-run batch columns. It exists solely to
    provide a stable cross-turn chain for sequential metrics, plus the single conversation pass."""
    reader = _FakeReader({"t1": [_ok_span("t1")], "t2": [_ok_span("t2")]}, ["t1", "t2"])

    class _Registry:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def dispatch(self, kind, config, score_name, level, ctx):
            self.calls.append(score_name)
            from tracely.domain.evaluation.results import EvalResult

            return [EvalResult(score_name, level, "PASS", value=1.0)]

    registry = _Registry()
    svc = EvaluationService(trace_reader=reader, score_writer=_FakeWriter(), registry=registry)  # type: ignore[arg-type]
    specs = [
        {"id": "batch", "kind": "llm_judge", "score_name": "batch", "level": "AGENT_RUN", "config": {}},
        {"id": "seq", "kind": "llm_judge", "score_name": "seq", "level": "AGENT_RUN",
         "config": {"execution_mode": "sequential"}},
        {"id": "conv", "kind": "llm_judge", "score_name": "conv", "level": "CONVERSATION", "config": {}},
    ]

    out = svc.evaluate_thread("p", "th-1", specs=specs, execution_mode="sequential")

    assert out == {"scores": 3, "failures": 0}
    assert registry.calls == ["seq", "seq", "conv"]


def test_conversation_targeting_uses_the_thread_as_the_sampling_subject():
    """Conversation metrics match any target turn, but their sampling decision stays stable for
    the conversation itself rather than changing with whichever turn arrived last."""
    spans = [_ok_span("t1")]
    spans[0]["env"] = "prod"
    svc = EvaluationService(trace_reader=_FakeReader({"t1": spans}, ["t1"]), score_writer=_FakeWriter())  # type: ignore[arg-type]
    specs = [
        {"score_name": "keep", "target_agent": "", "target_env": "prod", "sampling": 1.0},
        {"score_name": "wrong-env", "target_agent": "", "target_env": "ci", "sampling": 1.0},
        {"score_name": "never", "target_agent": "", "target_env": "prod", "sampling": 0.0},
    ]

    got = svc._apply_conversation_targeting("p", specs, spans, "th-1")

    assert [s["score_name"] for s in got] == ["keep"]


# ── a whole-thread pass writes whole-thread scores, or nothing ────────────────


def test_the_conversation_pass_drops_span_scoped_results(capsys):
    """This pass writes with NO trace_id, so only CONVERSATION-level results are addressable —
    readers find them by session_id. A span-scoped evaluator misconfigured to CONVERSATION level
    (`tool_success` keeps its TOOL level) would otherwise be written with an empty trace_id: rows
    no query in the system can reach, silently."""
    from tracely.domain.evaluation.results import EvalResult
    from tracely.services.evaluation_service import EvaluationService

    written: list = []
    svc = EvaluationService.__new__(EvaluationService)
    svc.trace_reader = type("R", (), {
        "read_thread_spans": staticmethod(
            lambda p, th: [{"span_id": "s1", "parent_span_id": "", "trace_id": "tr-1"}]
        )
    })()
    svc.score_writer = type("W", (), {
        "write_eval_scores": staticmethod(lambda *a, **kw: written.append(a[3]))
    })()
    svc._dispatch_specs = lambda specs, ctx, ran=None: [
        EvalResult("conv.judge", "CONVERSATION", "PASS"),
        EvalResult("tool.success", "TOOL", "FAIL", target_span_id="s1"),
    ]

    assert svc._evaluate_conversation("p1", "th-1", [{"score_name": "x"}], None) == 1
    assert [r.name for r in written[0]] == ["conv.judge"]
    # Dropped, but never silently — structlog prints to stdout, so capsys is what sees it.
    assert "tool.success" in capsys.readouterr().out


def test_thread_pass_marks_the_chain_and_resets_its_conversations(monkeypatch):
    """`evaluate_thread` IS the ordered whole-thread pass: it stamps `__chain_pass__` on
    sequential specs (the one licence to extend a message-level judge's durable conversation)
    and resets those conversations first, so each pass rebuilds from turn 1 instead of appending
    a second copy of every turn to what the previous pass left."""
    from tracely.infrastructure.llm import checkpointer

    seq = {
        "id": "ev-2", "kind": "llm_judge", "config": {"execution_mode": "sequential"},
        "score_name": "helpfulness", "level": "AGENT_RUN",
    }
    step_seq = {
        "id": "ev-3", "kind": "llm_judge", "config": {"execution_mode": "sequential"},
        "score_name": "tool_choice", "level": "TOOL",
    }
    resets: list[str] = []
    monkeypatch.setattr(checkpointer, "reset_chat", resets.append)
    staged: list[list[dict]] = []

    def fake_trace(self, project_id, trace_id, specs=None, **kw):
        staged.append(specs)
        return {"scores": 0, "failures": 0, "ran": [s["score_name"] for s in specs or []]}

    monkeypatch.setattr(EvaluationService, "evaluate_trace", fake_trace)
    svc = EvaluationService(trace_reader=type("R", (), {
        "thread_trace_ids": lambda self, p, t: ["t1", "t2"],
        "read_thread_spans": lambda self, p, t: [],
    })())
    svc.evaluate_thread("p1", "th-1", specs=[dict(_SPEC), seq, step_seq])

    # only the message-level judge's conversation lives on the thread subject; the step judge
    # resets its own per-trace conversation inside its pass
    assert resets == ["p1:helpfulness:th-1"]
    for specs in staged:
        by_name = {s["score_name"]: s for s in specs}
        assert by_name["helpfulness"]["config"].get("__chain_pass__") is True
        assert by_name["tool_choice"]["config"].get("__chain_pass__") is True
        assert "__chain_pass__" not in by_name["tracely.run.outcome"]["config"]


class _Reader:
    def __init__(self, turn_ids):
        self._turns = turn_ids

    def thread_trace_ids(self, project_id, thread_id):
        return list(self._turns)

    def read_thread_spans(self, project_id, thread_id):
        return []


def _seq_spec(name="helpfulness", level="AGENT_RUN"):
    return {
        "id": f"ev-{name}", "kind": "llm_judge",
        "config": {"execution_mode": "sequential"}, "score_name": name, "level": level,
    }


def _chain_harness(monkeypatch, progress: dict):
    """Stub the chain-progress persistence + chat reset + lock around evaluate_thread."""
    from tracely.infrastructure.db import repositories
    from tracely.infrastructure.llm import checkpointer
    from tracely.infrastructure.queue import thread_lock
    from tracely.services import evaluation_service

    calls = {"set": [], "clear": [], "reset": [], "graded": []}
    monkeypatch.setattr(
        repositories, "chain_progress_load", lambda s, p, t: dict(progress)
    )
    monkeypatch.setattr(
        repositories, "chain_progress_set",
        lambda s, p, name, t, turn_ids, payload: calls["set"].append((name, turn_ids, payload)),
    )
    monkeypatch.setattr(
        repositories, "chain_progress_clear",
        lambda s, p, t, names=None: calls["clear"].append(names),
    )
    monkeypatch.setattr(checkpointer, "reset_chat", lambda cid: calls["reset"].append(cid))

    class _NullSession:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(evaluation_service, "SyncSessionLocal", _NullSession)

    import contextlib

    monkeypatch.setattr(
        thread_lock, "thread_pass_lock",
        lambda p, t: contextlib.nullcontext(calls.setdefault("locked", True)),
    )

    def fake_trace(self, project_id, trace_id, specs=None, **kw):
        calls["graded"].append((trace_id, specs))
        return {"scores": 1, "failures": 0, "ran": [s["score_name"] for s in specs or []]}

    monkeypatch.setattr(EvaluationService, "evaluate_trace", fake_trace)
    return calls


def test_automatic_pass_grades_only_the_new_turns(monkeypatch):
    """The settled-thread pass is incremental: turns already on the column's conversation are
    skipped, the new turn is graded seeded with the persisted last payload, and progress
    advances — no reset, no re-grade of turn 1."""
    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: [_seq_spec()]),
    )
    calls = _chain_harness(monkeypatch, {
        "helpfulness": {"turn_ids": ["t1"], "last_payload": {"verdict": "PASS", "value": 0.9}},
    })
    svc = EvaluationService(trace_reader=_Reader(["t1", "t2"]))
    r = svc.evaluate_thread("p1", "th-1")

    assert [tid for tid, _ in calls["graded"]] == ["t2"]  # t1 already chained
    (_, specs), = calls["graded"]
    (spec,) = specs
    assert spec["config"]["__chain_pass__"] is True
    assert spec["config"]["__previous_result__"] == {"verdict": "PASS", "value": 0.9}
    assert calls["reset"] == [] and calls["clear"] == []
    assert calls["set"] == [("helpfulness", ["t1", "t2"], {"verdict": "PASS", "value": 0.9})]
    assert r["scores"] == 1


def test_reordered_turns_force_a_rebuild_from_turn_one(monkeypatch):
    """A stored prefix that no longer matches the thread's turn order (late-arriving trace) can't
    be continued — the conversation resets and every turn re-grades."""
    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: [_seq_spec()]),
    )
    calls = _chain_harness(monkeypatch, {
        "helpfulness": {"turn_ids": ["t9"], "last_payload": {"verdict": "PASS"}},
    })
    svc = EvaluationService(trace_reader=_Reader(["t1", "t2"]))
    svc.evaluate_thread("p1", "th-1")

    assert [tid for tid, _ in calls["graded"]] == ["t1", "t2"]
    assert calls["reset"] == ["p1:helpfulness:th-1"]
    assert calls["clear"] == [["helpfulness"]]
    # the stale payload is NOT seeded into the rebuilt turn 1
    first_spec = calls["graded"][0][1][0]
    assert "__previous_result__" not in first_spec["config"]


def test_lock_contention_and_redis_down_fail_open(monkeypatch):
    """The lock is best-effort: contention past the wait, or Redis down, still runs the pass —
    an eval that never runs is worse than a rare interleave (which progress heals next pass)."""
    from tracely.infrastructure.queue import eval_debounce, thread_lock

    class _Held:
        def set(self, *a, **k):
            return False  # someone else holds it

        def eval(self, *a, **k):
            raise AssertionError("must not release a lock it never acquired")

    monkeypatch.setattr(eval_debounce, "_get_client", lambda: _Held())
    monkeypatch.setattr(thread_lock, "_WAIT_SECONDS", 0)
    with thread_lock.thread_pass_lock("p1", "th-1"):
        pass  # reached without the lock

    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(eval_debounce, "_get_client", boom)
    with thread_lock.thread_pass_lock("p1", "th-1"):
        pass


def test_lock_acquire_and_release(monkeypatch):
    from tracely.infrastructure.queue import eval_debounce, thread_lock

    ops: list = []

    class _Free:
        def set(self, key, token, nx=None, ex=None):
            ops.append(("set", key, token))
            return True

        def eval(self, script, n, key, token):
            ops.append(("release", key, token))
            return 1

    monkeypatch.setattr(eval_debounce, "_get_client", lambda: _Free())
    with thread_lock.thread_pass_lock("p1", "th-1"):
        pass
    assert [o[0] for o in ops] == ["set", "release"]
    assert ops[0][1] == ops[1][1] and ops[0][2] == ops[1][2]  # same key, same token


# ── a re-run replaces a column's results; it doesn't only add ─────────────────


def test_a_rerun_retracts_rows_the_column_no_longer_produces():
    from tracely.domain.evaluation.results import EvalResult

    calls = {}

    class Writer:
        def write_eval_scores(self, *a, **k):
            calls["write"] = a

        eval_score_id = staticmethod(__import__(
            "tracely.infrastructure.clickhouse.score_writer", fromlist=["ScoreWriter"]).ScoreWriter.eval_score_id)

        def retract_eval_scores(self, project_id, names, **kw):
            calls["retract"] = (names, kw)

    spans = [_ok_span("t1")]
    svc = EvaluationService(trace_reader=_FakeReader({"t1": spans}, ["t1"]), score_writer=Writer())  # type: ignore[arg-type]
    svc._dispatch_specs = lambda specs, ctx, ran=None: (ran.extend(["col"]) if ran is not None else None) or [
        EvalResult("col", "SPAN", "PASS", target_span_id="s2")
    ]
    svc.evaluate_trace("p", "t1", specs=[{"score_name": "col", "level": "SPAN", "kind": "structural", "config": {}}])
    names, kw = calls["retract"]
    assert names == ["col"] and kw["trace_id"] == "t1"
    # only the row just written survives
    assert kw["keep_ids"] == {Writer.eval_score_id("t1", kw["thread_id"], "col", "SPAN", "s2")}



def test_a_failed_sequential_grade_halts_its_chain_instead_of_skipping_the_turn(monkeypatch):
    """A 429 / provider error on turn 1 must not mark turn 1 done: progress stays behind it and
    turn 2 is not graded out of order; the next pass resumes at turn 1."""
    monkeypatch.setattr(
        EvaluationService, "load_enabled_evaluators",
        staticmethod(lambda project_id, evaluator_ids=None: [_seq_spec()]),
    )
    calls = _chain_harness(monkeypatch, {})

    def erroring_trace(self, project_id, trace_id, specs=None, on_result=None, **kw):
        calls["graded"].append((trace_id, specs))
        on_result({"name": "helpfulness", "graded": False, "error": True, "verdict": "", "value": None,
                   "comment": "Not graded: 429", "string_value": "Error"})
        return {"scores": 1, "failures": 0, "ran": ["helpfulness"]}

    monkeypatch.setattr(EvaluationService, "evaluate_trace", erroring_trace)
    svc = EvaluationService(trace_reader=_Reader(["t1", "t2"]))
    svc.evaluate_thread("p1", "th-1")
    assert [tid for tid, _ in calls["graded"]] == ["t1"]  # t2 not graded on a broken chain
    assert calls["set"] == []  # and t1 is NOT recorded as done
