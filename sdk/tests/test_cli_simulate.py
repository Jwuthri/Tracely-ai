"""`tracely simulate` — polling, exit codes, and the reason text a reviewer actually reads.

No network: the two API helpers are patched. What matters here is that a non-green run blocks the
merge and says why, since that is the whole job of the command.
"""

from __future__ import annotations

import time

import pytest

from tracely_sdk import cli


# ── reason text ───────────────────────────────────────────────────────────────


def test_transport_failure_is_the_reason():
    assert "Connection refused" in cli.case_reason(
        {"turns": 1, "error": "ConnectError: Connection refused"}
    )


def test_failed_expectations_are_listed():
    reason = cli.case_reason(
        {"failed_expectations": ["turn 2: never offered a refund", "turn 3: wrong total"]}
    )
    assert "never offered a refund" in reason and "wrong total" in reason


def test_failed_evaluators_are_listed():
    """A conversation that sank on the project's own evaluators must still name them, or the PR
    comment shows a red row with no cause."""
    assert "correctness" in cli.case_reason(
        {"failed_scores": ["correctness: invented a refund policy"]}
    )


def test_regression_case_reasons_still_work():
    """The scenario branch must not have broken the replay path's diagnostics."""
    reason = cli.case_reason({"missing_tools": ["get_weather"], "tools_ok": False})
    assert "get_weather" in reason


def test_ungraded_explains_itself():
    note = cli.ungraded_note("UNGRADED", {"turns": 3})
    assert "nothing scored it" in note and "never as a pass" in note


def test_ungraded_note_is_empty_for_other_verdicts():
    assert cli.ungraded_note("FAIL", {"turns": 3}) == ""


# ── markdown ──────────────────────────────────────────────────────────────────


def _data(status="FAIL", **over):
    d = {
        "id": "g1", "agent": "planner", "env": "ci", "status": status,
        "passed": 0, "failed": 1, "skipped": 0, "total": 1,
        "cases": [{
            "title": "Refund flow", "verdict": "FAIL", "scenario_id": "sc1",
            "candidate_trace_id": "conv123",
            "detail": {"turns": 3, "failed_expectations": ["turn 2: no refund offered"]},
        }],
    }
    d.update(over)
    return d


def test_markdown_links_the_conversation():
    """A reviewer should be able to read what was actually said, not just take the verdict's
    word for it."""
    md = cli.render_markdown(_data(), "https://tracely.test", "abc1234")
    assert "https://tracely.test/sessions/conv123" in md
    assert "no refund offered" in md


def test_markdown_does_not_link_a_replayed_case():
    """Only emulated conversations have a thread to open; a regression case must not get a
    /sessions link built from its candidate trace."""
    data = _data()
    data["cases"][0].pop("scenario_id")
    md = cli.render_markdown(data, "https://tracely.test", "abc1234")
    assert "/sessions/" not in md


def test_console_render_does_not_crash_on_an_ungraded_case(capsys):
    data = _data(status="NO_COVERAGE")
    data["cases"][0]["verdict"] = "UNGRADED"
    cli.render_console(data, "abc1234")
    out = capsys.readouterr().out
    assert "UNGRADED" in out and "NO COVERAGE" in out


# ── polling + exit codes ──────────────────────────────────────────────────────


def _args(**over):
    import argparse

    base = dict(
        agent="planner", agents=None, all_agents=False, env="ci", api="http://api.test", key="k",
        web_url="", pr=None, sha="deadbeef", github=False, no_github=True, token="",
        dry_run=True, min_pass_rate=None, timeout=30,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_poll_returns_once_the_gate_finishes(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, key):
        calls["n"] += 1
        return {"id": "g1", "finished_at": None} if calls["n"] < 2 else {"id": "g1", "finished_at": "now"}

    # `poll_gate` imports time locally, so patch the module itself.
    monkeypatch.setattr(cli, "_get_json", fake_get)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    assert cli.poll_gate("http://api.test", "k", "g1", timeout=30, quiet=True)["finished_at"]
    assert calls["n"] == 2


def test_poll_raises_rather_than_reporting_green(monkeypatch):
    """A gate that never finished is not a pass. Timing out has to surface, not fall through."""
    monkeypatch.setattr(cli, "_get_json", lambda url, key: {"id": "g1", "finished_at": None})
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        cli.poll_gate("http://api.test", "k", "g1", timeout=0, quiet=True)


@pytest.mark.parametrize(
    "status,code",
    # 1 = the gate said no · 2 = we never got a verdict (a server-side ERROR is an infra
    # failure, not a red suite — same bucket as timeout/unreachable, per the module docstring).
    [("PASS", 0), ("FAIL", 1), ("NO_COVERAGE", 1), ("ERROR", 2)],
)
def test_exit_code_blocks_the_merge_on_anything_but_pass(monkeypatch, status, code):
    monkeypatch.setattr(cli, "start_simulation", lambda *a, **k: {"id": "g1"})
    monkeypatch.setattr(cli, "poll_gate", lambda *a, **k: _data(status=status))
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args()) == code


def test_a_timeout_exits_non_zero(monkeypatch):
    monkeypatch.setattr(cli, "start_simulation", lambda *a, **k: {"id": "g1"})

    def boom(*a, **k):
        raise TimeoutError("gate g1 did not finish within 30s")

    monkeypatch.setattr(cli, "poll_gate", boom)
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args()) == 2


def test_missing_agent_is_a_usage_error(monkeypatch):
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    assert cli.cmd_simulate(_args(agent=None)) == 2


# ── choosing which agents to gate ────────────────────────────────────────────


def test_agents_can_be_repeated_or_comma_separated(monkeypatch):
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    assert cli.agent_list(_args(agent=None, agents=["a", "b,c"])) == ["a", "b", "c"]


def test_the_same_agent_is_never_gated_twice(monkeypatch):
    """Positional plus --agent naming the same slug must not start two runs for it."""
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    assert cli.agent_list(_args(agent="planner", agents=["planner", "support"])) == [
        "planner", "support"
    ]


def test_discover_skips_agents_whose_scenarios_are_all_disabled(monkeypatch):
    """A disabled suite has nothing to run — gating it would report NO_COVERAGE and block the PR
    for a test nobody wrote."""
    monkeypatch.setattr(cli, "_get_json", lambda url, key: [
        {"agent": "planner", "enabled": True},
        {"agent": "planner", "enabled": True},   # deduped
        {"agent": "muted", "enabled": False},
        {"agent": "support", "enabled": True},
    ])
    assert cli.discover_agents("http://api.test", "k") == ["planner", "support"]


def test_all_with_nothing_to_run_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    monkeypatch.setattr(cli, "_get_json", lambda url, key: [])
    assert cli.cmd_simulate(_args(agent=None, all_agents=True)) == 2


def test_all_gates_every_discovered_agent(monkeypatch):
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    monkeypatch.setattr(cli, "discover_agents", lambda api, key: ["planner", "support"])
    started = []
    monkeypatch.setattr(
        cli, "start_simulation",
        lambda api, key, agent, *a, **k: started.append(agent) or {"id": f"g-{agent}"},
    )
    monkeypatch.setattr(cli, "poll_gate", lambda api, key, gid, *a, **k: _data(status="PASS"))
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args(agent=None, all_agents=True)) == 0
    assert started == ["planner", "support"]


def test_all_overrides_an_ambient_tracely_agent(monkeypatch):
    """CI envs routinely export TRACELY_AGENT. `--all` must still gate every agent — silently
    shrinking to the env var's one agent exits 0 having tested a fraction of the suite."""
    monkeypatch.setenv("TRACELY_AGENT", "planner")
    monkeypatch.setattr(cli, "discover_agents", lambda api, key: ["planner", "support"])
    started = []
    monkeypatch.setattr(
        cli, "start_simulation",
        lambda api, key, agent, *a, **k: started.append(agent) or {"id": f"g-{agent}"},
    )
    monkeypatch.setattr(cli, "poll_gate", lambda api, key, gid, *a, **k: _data(status="PASS"))
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args(agent=None, all_agents=True)) == 0
    assert started == ["planner", "support"]


def test_one_red_agent_blocks_the_whole_run(monkeypatch):
    """The merge gate is the worst result across agents — a green one must never mask a red one."""
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    verdicts = iter(["PASS", "FAIL"])
    monkeypatch.setattr(cli, "start_simulation", lambda *a, **k: {"id": "g1"})
    monkeypatch.setattr(cli, "poll_gate", lambda *a, **k: _data(status=next(verdicts)))
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args(agent=None, agents=["good", "bad"])) == 1


def test_a_timed_out_agent_does_not_abandon_the_others(monkeypatch):
    """One agent hanging must still leave the rest reported — and must still block."""
    monkeypatch.delenv("TRACELY_AGENT", raising=False)
    polled = []

    def poll(api, key, gate_id, timeout, **k):
        polled.append(gate_id)
        if gate_id == "g-slow":
            raise TimeoutError("nope")
        return _data(status="PASS")

    monkeypatch.setattr(
        cli, "start_simulation", lambda api, key, agent, *a, **k: {"id": f"g-{agent}"}
    )
    monkeypatch.setattr(cli, "poll_gate", poll)
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    assert cli.cmd_simulate(_args(agent=None, agents=["slow", "quick"])) == 2
    assert polled == ["g-slow", "g-quick"]


def test_one_comment_covers_every_agent():
    """GitHub keys our comment by a hidden marker, so N comments would overwrite each other —
    a red agent could vanish behind a green one that finished later."""
    md = cli.render_markdown_all(
        [_data(status="PASS", agent="planner"), _data(status="FAIL", agent="support")],
        "https://tracely.test", "abc1234",
    )
    assert md.count(cli.MARKER) == 1
    assert "planner" in md and "support" in md
    assert "**FAIL**" in md  # the headline is the worst agent, not the first


def test_single_agent_markdown_is_unchanged():
    one = _data()
    assert cli.render_markdown_all([one], "https://tracely.test", "abc1234") == cli.render_markdown(
        one, "https://tracely.test", "abc1234"
    )


def test_min_pass_rate_is_forwarded(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        cli, "start_simulation",
        lambda api, key, agent, env, ref, pr, mpr=None: sent.update(rate=mpr) or {"id": "g1"},
    )
    monkeypatch.setattr(cli, "poll_gate", lambda *a, **k: _data(status="PASS"))
    monkeypatch.setattr(cli, "gh_context", lambda: ("", "", None))

    cli.cmd_simulate(_args(min_pass_rate=0.9))
    assert sent["rate"] == 0.9


def test_conn_treats_empty_env_as_unset(monkeypatch):
    """An unset GitHub secret arrives as TRACELY_API="" — the default must still apply."""
    from argparse import Namespace

    from tracely_sdk.cli import _conn

    monkeypatch.setenv("TRACELY_API", "")
    monkeypatch.setenv("TRACELY_KEY", "")
    args = Namespace(api=None, key=None, web_url=None, agent="planner")
    api, key, _, agent = _conn(args)
    assert api == "http://localhost:8000"
    assert key == "tracely_dev_key"
    assert agent == "planner"


def test_auth_hint_separates_a_missing_key_from_a_stale_one():
    """A 401 is `invalid ingest key` either way, and the two causes need opposite fixes: set the
    secret, or refresh it. Getting this wrong cost a real CI session — the log read like the key
    was bad when the gate had simply never been given one."""
    from tracely_sdk.cli import DEV_KEY, _auth_hint

    missing = _auth_hint("https://api.example.com", DEV_KEY)
    assert "unset" in missing
    assert "https://api.example.com" in missing

    stale = _auth_hint("https://api.example.com", "tk_rotated")
    assert "rotated" in stale
    assert "unset" not in stale, "a key that IS set must not be reported as missing"


# ── GitHub API timeout ──────────────────────────────────────────────────────


def test_github_call_passes_the_socket_timeout(monkeypatch):
    """Without a timeout a hung GitHub connection hangs the CI job forever with no output
    and no exit code (issue #90) — every `GitHub._call` must carry `_HTTP_TIMEOUT_S`."""
    import io

    calls = {}

    class _Resp(io.BytesIO):
        length = 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, **kw):
        calls.update(kw)
        return _Resp(b"{}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    assert cli.GitHub("tok")._call("POST", "/repos/o/r/statuses/abc", {"state": "success"}) == {}
    assert calls.get("timeout") == cli._HTTP_TIMEOUT_S == 60
