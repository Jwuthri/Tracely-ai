"""`tracely replay --entrypoint` under the strict recorded policy (W1).

No network: the suite fetch, gate trigger and trace wait are patched. What is pinned is the
execution contract the exit code rests on — a case whose recording is unavailable, or whose run
asked for a call the recording lacks, produces an ERRORED trace (never a live or green one), and
a clean recorded run is labelled as such."""

from __future__ import annotations

import argparse
import sys
import types

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracely_sdk as tracely
from tracely_sdk import cli

RAN: list[str] = []


def _agent(user_input: str) -> str:
    RAN.append(user_input)
    return tracely.call_tool("lookup", lambda: pytest.fail("live"), args={"q": user_input})


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    tracely.init(env="ci", instrument=False)
    exp = InMemorySpanExporter()
    tracely._provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp


@pytest.fixture(autouse=True)
def _wire(monkeypatch, exporter):
    exporter.clear()
    RAN.clear()
    mod = types.ModuleType("fake_agent")
    mod.run = _agent
    monkeypatch.setitem(sys.modules, "fake_agent", mod)
    # init() is process-global; keep the module exporter and make the CLI's re-init a no-op.
    monkeypatch.setattr(tracely, "init", lambda **kw: None)
    monkeypatch.setattr(cli, "_wait_for_traces", lambda *a, **k: True)
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))
    monkeypatch.setattr(cli, "write_step_summary", lambda md: None)
    monkeypatch.setattr(cli, "post_pr_check", lambda *a, **k: None)
    monkeypatch.setattr(cli, "render_console", lambda *a, **k: None)
    monkeypatch.setattr(cli, "render_markdown", lambda *a, **k: "")
    yield
    exporter.clear()


def _run(monkeypatch, cases: list[dict], **flags) -> tuple[int, dict]:
    captured: dict = {}
    monkeypatch.setattr(cli, "_get_json", lambda url, key: {"cases": cases})

    def trigger(api, key, agent, env, sha, pr, candidates=None, **kw):
        captured["candidates"] = candidates
        captured.update(kw)
        return {"status": "FAIL"}

    monkeypatch.setattr(cli, "trigger_gate", trigger)
    args = argparse.Namespace(
        **{
            "api": "http://x", "key": "k", "web_url": "", "agent": "planner",
            "entrypoint": "fake_agent:run", "cmd": None, "live": False, "lenient": False,
            "env": "ci", "sha": "", "pr": None, "github": False, "no_github": True, **flags,
        }
    )
    return cli.cmd_replay(args), captured


def _root(exporter: InMemorySpanExporter):
    return next(s for s in exporter.get_finished_spans() if s.name == "planner")


def _case(fixtures, error=None) -> dict:
    return {"id": "c1", "title": "case", "input": "hi", "fixtures": fixtures, "fixture_error": error}


def test_clean_recorded_run(monkeypatch, exporter, capsys):
    bundle = {"version": 2, "tools": [{"name": "lookup", "args": '{"q": "hi"}', "output": "rec", "error": None}]}
    _, cap = _run(monkeypatch, [_case(bundle)])
    assert RAN == ["hi"] and cap["candidates"] == {"c1": _root(exporter).context.trace_id.__format__("032x")}
    root = _root(exporter)
    assert root.status.status_code.name != "ERROR"
    attrs = dict(root.attributes)
    assert attrs["tracely.replay.mode"] == "recorded"
    # provenance: the run id minted for this execution is on the trace AND in the gate request
    assert attrs["tracely.replay.run_id"] == cap["run_id"] and cap["run_id"].startswith("run-")
    assert cap["execution_mode"] == "recorded" and cap["failed"] is None
    assert "[recorded · 1 served]" in capsys.readouterr().out


def test_unloadable_fixtures_never_run_the_agent(monkeypatch, exporter, capsys):
    _run(monkeypatch, [_case(None, error="fixture bundle k could not be loaded: NoSuchKey")])
    assert RAN == []  # the agent did not run live on a missing recording
    root = _root(exporter)
    assert root.status.status_code.name == "ERROR" and "NoSuchKey" in root.status.description
    assert "fixtures unavailable" in capsys.readouterr().out


def test_strict_miss_errors_the_trace(monkeypatch, exporter, capsys):
    _run(monkeypatch, [_case({"version": 2, "tools": []})])  # explicitly empty recording
    root = _root(exporter)
    assert root.status.status_code.name == "ERROR"
    assert "no recorded call for tools:lookup" in root.status.description
    out = capsys.readouterr().out
    assert "diverged" in out and "no recorded call" in out


def test_live_flag_ignores_recording_and_says_so(monkeypatch, exporter, capsys):
    mod = sys.modules["fake_agent"]
    mod.run = lambda inp: tracely.call_tool("lookup", lambda: "LIVE")
    _run(monkeypatch, [_case(None, error="whatever")], live=True)
    root = _root(exporter)
    assert root.status.status_code.name != "ERROR"
    assert dict(root.attributes)["tracely.replay.mode"] == "live"
    assert "[live]" in capsys.readouterr().out


async def _async_agent(user_input: str) -> str:
    RAN.append(user_input)
    return "async-ok"


def test_async_entrypoint_is_awaited(monkeypatch, exporter):
    sys.modules["fake_agent"].run = _async_agent
    _, cap = _run(monkeypatch, [_case(None, error="x")], live=True)
    assert RAN == ["hi"]
    assert _root(exporter).attributes["tracely.output"] == "async-ok"  # the answer, not a coroutine


def test_generator_entrypoint_is_refused(monkeypatch):
    def gen(user_input):
        yield "x"

    sys.modules["fake_agent"].run = gen
    with pytest.raises(SystemExit, match="generator"):
        _run(monkeypatch, [_case(None, error="x")], live=True)


def test_explicit_run_id_is_used(monkeypatch, exporter):
    _, cap = _run(monkeypatch, [_case({"version": 2, "tools": [{"name": "lookup", "args": None, "output": "r", "error": None}]})], run_id="gh-42-1")
    assert cap["run_id"] == "gh-42-1"
    assert dict(_root(exporter).attributes)["tracely.replay.run_id"] == "gh-42-1"


def test_cmd_path_records_failed_commands_and_polls_by_run(monkeypatch, capsys):
    polls: list[str] = []
    monkeypatch.setattr(cli, "_get_json", lambda url, key: (polls.append(url) or {"cases": [
        {"id": "c1", "title": "ok", "input": "a", "fixtures": None, "fixture_error": None},
        {"id": "c2", "title": "boom", "input": "b", "fixtures": None, "fixture_error": None},
    ]}) if "suite" in url else (polls.append(url) or {"trace_ids": ["t1"]}))
    captured: dict = {}

    def trigger(api, key, agent, env, sha, pr, candidates=None, **kw):
        captured.update(kw, candidates=candidates)
        return {"status": "INCOMPLETE"}

    monkeypatch.setattr(cli, "trigger_gate", trigger)
    args = argparse.Namespace(
        api="http://x", key="k", web_url="", agent="planner", entrypoint=None, live=False, lenient=False,
        env="ci", sha="", pr=None, github=False, no_github=True, run_id=None, cmd_timeout=5, timeout=1,
        cmd='[ "$TRACELY_INPUT" = "a" ] && [ -n "$TRACELY_RUN_ID" ]',  # exits 1 for case b
    )
    code = cli.cmd_replay(args)
    assert code == 2  # INCOMPLETE → no verdict
    assert captured["failed"] == {"c2": "command exited 1"}
    assert captured["candidates"] is None and captured["execution_mode"] == "live"
    assert any("run-traces" in u and captured["run_id"] in u for u in polls)  # polled, not slept
    out = capsys.readouterr().out
    assert "boom  ->  exited 1" in out and "runs the agent live" in out


def test_case_flag_replays_one_case_and_scopes_the_gate(monkeypatch, capsys):
    cases = [
        _case({"version": 2, "tools": [{"name": "lookup", "args": None, "output": "r", "error": None}]}),
        {**_case(None, error="x"), "id": "c2", "title": "other"},
    ]
    _, cap = _run(monkeypatch, cases, case="c1")
    assert RAN == ["hi"] and cap["case_ids"] == ["c1"] and set(cap["candidates"]) == {"c1"}
    assert "other" not in capsys.readouterr().out
    assert cli.cmd_replay(argparse.Namespace(**{
        "api": "http://x", "key": "k", "web_url": "", "agent": "planner", "entrypoint": "fake_agent:run",
        "cmd": None, "live": False, "lenient": False, "env": "ci", "sha": "", "pr": None, "github": False,
        "no_github": True, "case": "nope",
    })) == 2
