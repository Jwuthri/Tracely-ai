"""The assistant's toolbox, driven against the real app over an in-process ASGI transport.

Proves the three things this design rests on: a tool call goes through the real router and mutates
real state, the caller's own key is what scopes it, and a rejected write comes back as TEXT rather
than an exception — because an exception out of a tool kills the whole stream mid-answer, and the
router's own complaint is exactly what lets the agent correct itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from tracely.auth import tokens
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import assistant_tools

_TABLES = [
    models.Organization.__table__,
    models.OrgMembership.__table__,
    models.Project.__table__,
    models.IngestKey.__table__,
    models.User.__table__,
    models.Membership.__table__,
    models.Invitation.__table__,
    models.Evaluator.__table__,
    models.Monitor.__table__,
    models.MonitorStep.__table__,
]


@pytest.fixture
def tools():
    """The toolbox as a name → tool mapping, for a caller with `key`."""

    def _for(key: str | None):
        headers = {"authorization": f"Bearer {key}"} if key else {}
        return {t.name: t for t in assistant_tools.build_tools(headers)}

    return _for


@pytest.fixture
async def engine(tmp_path):
    """File-backed (overrides conftest's :memory:) so the evaluators router's SYNC sessionmaker
    sees the same database — same reason as test_mcp_server."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    import tracely.api.routers.evaluators as evaluators_router
    import tracely.api.routers.monitors as monitors_router

    maker = sessionmaker(create_engine(f"sqlite:///{tmp_path}/test.db"))
    monkeypatch.setattr(evaluators_router, "SyncSessionLocal", maker)
    monkeypatch.setattr(monitors_router, "SyncSessionLocal", maker)


async def test_a_write_tool_mutates_real_state(client, sync_db, make_workspace, tools):
    """The whole point: no second implementation of anything — the tool is the endpoint."""
    _proj, user, _key = await make_workspace("tools-crud", "tools_key_crud", "crud@x.test")
    # As the signed-in user, the way the dashboard runs it: the delete at the end is a
    # `require_user` route, which an ingest key can't reach (test_ingest_key_scope.py).
    t = tools(tokens.issue_session(user.id))

    created = await t["create_evaluator"].ainvoke(
        {
            "name": "Refund policy",
            "kind": "llm_judge",
            "level": "AGENT_RUN",
            "evaluator_config": {"prompt": "Did the agent follow the refund policy?", "threshold": 0.6},
        }
    )
    assert created["name"] == "Refund policy"

    listed = await t["list_evaluators"].ainvoke({})
    assert created["id"] in {e["id"] for e in listed}

    deleted = await t["delete_evaluator"].ainvoke({"evaluator_id": created["id"]})
    assert deleted == {"deleted": created["id"]}
    assert await t["list_evaluators"].ainvoke({}) == []


async def test_a_rejected_write_comes_back_as_text_not_an_exception(
    client, sync_db, make_workspace, tools
):
    """langgraph re-raises anything that isn't a bad-args error, so a raising tool ends the turn
    with a dead chat. Returned, the router's own 400 is the agent's feedback loop."""
    await make_workspace("tools-bad", "tools_key_bad", "bad@x.test")
    t = tools("tools_key_bad")

    out = await t["create_evaluator"].ainvoke(
        {"name": "Nope", "kind": "structural", "evaluator_config": {"check": "bogus"}}
    )
    assert isinstance(out, str)
    assert out.startswith("error: Tracely API 400")
    assert "unknown structural check" in out  # the model can act on this


async def test_create_alert_builds_the_condition_the_backend_expects(
    client, sync_db, make_workspace, tools
):
    """The tool's flat arguments have to land as the nested `condition` the router validates —
    and only the keys THIS trigger uses, or the alert carries filters nobody set."""
    _proj, user, _key = await make_workspace("tools-alert", "tools_key_alert", "alert@x.test")
    t = tools(tokens.issue_session(user.id))

    made = await t["create_alert"].ainvoke(
        {
            "name": "PII in production",
            "trigger": "trace_failed",
            "contains": "pii",
            "destination": "oncall@acme.com",
            "action": "email",
            "min_interval_seconds": 0,
        }
    )
    assert made["condition"] == {"type": "trace_failed", "contains": "pii"}
    # The tool creates a real FLOW, so the rule the assistant makes is the same kind of object the
    # canvas edits — one mechanism, not a second notification path.
    assert [(s["step_type"], s["config"]["to_template"]) for s in made["steps"]] == [
        ("send_email", "oncall@acme.com")
    ]
    assert made["flow_layout"]["edges"][0]["source"] == "__rule_trigger__"

    threshold = await t["create_alert"].ainvoke(
        {
            "name": "Quality regression",
            "trigger": "fail_rate_over",
            "score_name": "tracely.run.quality",
            "threshold": 0.2,
            "destination": "https://hooks.slack.com/services/T/B/x",
        }
    )
    assert threshold["condition"] == {
        "type": "fail_rate_over",
        "score_name": "tracely.run.quality",
        "threshold": 0.2,
        "window_minutes": 60,
        "min_samples": 20,
    }
    assert {a["name"] for a in await t["list_alerts"].ainvoke({})} == {
        "PII in production", "Quality regression",
    }


async def test_an_ingest_key_cannot_point_an_alert_anywhere(client, sync_db, make_workspace, tools):
    """Creating an alert is a `require_user` route: a leaked CI key must not be able to aim a
    workspace's alerts at a channel of its own — and the refusal comes back as text."""
    await make_workspace("tools-alert-key", "tools_key_alert_ro", "alertkey@x.test")
    t = tools("tools_key_alert_ro")

    out = await t["create_alert"].ainvoke(
        {"name": "mine now", "trigger": "cluster_new", "destination": "https://hooks.slack.com/x"}
    )
    assert isinstance(out, str) and out.startswith("error: Tracely API 403")
    assert await t["list_alerts"].ainvoke({}) == []  # reading is still fine


async def test_no_tool_argument_is_named_config(tools):
    """`StructuredTool._arun` eats an argument called `config` — the tool then fails at call time,
    inside a live turn, with a missing-argument TypeError. Nothing catches that at import."""
    for name, t in tools("k").items():
        assert "config" not in t.args, f"{name} has a `config` argument; rename it"


async def test_tools_reach_exactly_the_callers_workspace(client, sync_db, make_workspace, tools):
    """No key → the routers' own 401, as text. Another workspace's key → only its own rows."""
    await make_workspace("tools-a", "tools_key_a", "a@x.test")
    await make_workspace("tools-b", "tools_key_b", "b@x.test")

    assert (await tools(None)["list_evaluators"].ainvoke({})).startswith("error: Tracely API 401")

    await tools("tools_key_a")["create_evaluator"].ainvoke(
        {"name": "A only", "evaluator_config": {"prompt": "x"}}
    )
    assert await tools("tools_key_b")["list_evaluators"].ainvoke({}) == []


async def test_the_toolbox_covers_the_product(tools):
    """A capability missing from this list is a capability the agent silently doesn't have."""
    names = set(tools("k"))
    assert {
        "get_trace", "get_conversation", "search_traces",  # answering "why did this fail"
        "create_evaluator", "run_evaluation",  # columns, and backfilling them
        "create_scenario", "import_scenario", "run_scenario",  # emulated conversations
        "promote_trace", "promote_cluster", "replay_case",  # regression cases
        "create_alert", "list_alerts",  # "tell me when this happens"
        "delete_conversations",  # the destructive end, gated by the system prompt
    } <= names


def test_an_oversized_result_is_clipped_with_a_way_out():
    """Unclipped, one export evicts the conversation it was meant to inform. The note matters as
    much as the cut: the model needs to know it saw a slice, and how to ask for less."""
    small = [{"id": 1}]
    assert assistant_tools._clip(small) is small

    clipped = assistant_tools._clip([{"body": "x" * assistant_tools.MAX_RESULT_CHARS}])
    assert isinstance(clipped, str)
    assert len(clipped) < assistant_tools.MAX_RESULT_CHARS + 200
    assert "truncated" in clipped and "Narrow the request" in clipped


async def test_a_backfill_is_capped_and_summarised(client, sync_db, make_workspace, monkeypatch):
    """The eval endpoint streams SSE and re-grades with real judge calls, on our event loop,
    inside one chat turn. The tool has to parse the frames and refuse an unbounded target list."""
    from tracely.api.routers import evaluations as evaluations_router

    asked: dict = {}

    class FakeService:
        @staticmethod
        def load_enabled_evaluators(project_id, ids):
            return [object()]  # one "enabled evaluator", enough for the router to proceed

        def evaluate_thread(self, project_id, target_id, *, specs, on_result):
            asked.setdefault("threads", []).append(target_id)
            on_result({"name": "helpfulness", "verdict": "PASS"})
            return {"scores": 1}

    monkeypatch.setattr(evaluations_router, "EvaluationService", FakeService)

    await make_workspace("tools-eval", "tools_key_eval", "eval@x.test")
    t = {x.name: x for x in assistant_tools.build_tools({"authorization": "Bearer tools_key_eval"})}

    out = await t["run_evaluation"].ainvoke(
        {"thread_ids": [f"t{i}" for i in range(assistant_tools.EVAL_TARGET_MAX + 10)]}
    )
    assert out["targets"] == assistant_tools.EVAL_TARGET_MAX
    assert out["scores_written"] == assistant_tools.EVAL_TARGET_MAX
    assert out["errors"] == []
    assert len(asked["threads"]) == assistant_tools.EVAL_TARGET_MAX

    assert await t["run_evaluation"].ainvoke({}) == "error: pass at least one thread_id or trace_id"


async def test_draft_alert_validates_and_only_draws_on_the_editor():
    """The browser applies this tool's ARGUMENTS, so a bad draft must be refused here — and a
    draft with no canvas open must say so rather than vanish."""
    slack = [{"name": "Post", "step_type": "slack", "config": {"url": "", "text_template": "x"}}]
    on_editor = {t.name: t for t in assistant_tools.build_tools({}, path="/settings/alerts/new")}
    elsewhere = {t.name: t for t in assistant_tools.build_tools({}, path="/traces")}

    out = await on_editor["draft_alert"].ainvoke(
        {"name": "PII", "trigger": "trace_failed", "steps": slack}
    )
    assert out["drawn"] is True and out["steps"] == 1
    assert "error" in await on_editor["draft_alert"].ainvoke(
        {"name": "x", "trigger": "trace_failed", "steps": [{"step_type": "teleport"}]}
    )
    assert "error" in await on_editor["draft_alert"].ainvoke(
        {"name": "x", "trigger": "nope", "steps": slack}
    )
    assert "create_alert" in await elsewhere["draft_alert"].ainvoke(
        {"name": "x", "trigger": "trace_failed", "steps": slack}
    )
