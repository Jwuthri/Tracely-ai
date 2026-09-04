"""The dashboard chat widget: what reaches the model, whose key pays, and what is stored.

The persistence half runs against a real SQLite database rather than a mocked repository — the
ownership rule (`user_id IS NULL` vs `= :uid`) is exactly the kind of thing a mock would agree
with while the SQL quietly matched nothing.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tracely.infrastructure.db import models
from tracely.infrastructure.llm import provider
from tracely.services import assistant_service as svc


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real database behind the service, so the repository's own SQL is what runs."""
    engine = create_engine(f"sqlite:///{tmp_path}/assistant.db")
    models.AssistantChat.__table__.create(engine)
    monkeypatch.setattr(svc, "SyncSessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    return engine


@pytest.fixture(autouse=True)
def no_real_chat_model(monkeypatch):
    """No test in this file may construct a real chat client.

    Stubbing `stream_agent` is not enough: `agent_middleware(...)` is an ARGUMENT to it, so it is
    evaluated first and eagerly builds the tool-picker's model — which raises without credentials.
    That is what turned CI red while every laptop stayed green.
    """
    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: _fake_chat_model())


def _stream(*events):
    """An async generator over `events` — what a stubbed `stream_agent` hands back."""

    async def gen():
        for e in events:
            yield e

    return gen()


@pytest.fixture
def model(monkeypatch):
    """A stubbed agent that records what it was asked. Returns the recording dict."""
    seen: dict = {}
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider,
        "stream_agent",
        lambda prompt, **kw: seen.update(prompt=prompt, **kw)
        or _stream({"type": "final", "text": "  hello  ", "usage": {}}),
    )
    return seen


async def turn(*args, **kw) -> dict:
    """Drive one turn to completion and return its terminal frame — the shape the widget acts on.

    Kept here rather than in the service: the streaming API is the one that ships, so the tests
    consume it the way the router does instead of testing a convenience wrapper nothing calls.
    """
    frames = [f async for f in svc.answer_stream(*args, **kw)]
    return frames[-1]


# ---------------------------------------------------------------- the prompt


def test_transcript_keeps_the_tail_oldest_first_and_names_the_page():
    msgs = [{"role": "user", "content": f"q{i}"} for i in range(svc.MAX_TURNS + 5)]
    msgs.append({"role": "assistant", "content": "an answer"})
    out = svc._transcript("p1", msgs, "/traces/abc")

    assert "/traces/abc" in out
    assert "q0" not in out  # the head is dropped, not the tail
    assert out.index(f"User: q{svc.MAX_TURNS + 4}") < out.index("Assistant: an answer")
    assert out.count("User:") + out.count("Assistant:") == svc.MAX_TURNS


def test_transcript_truncates_one_pasted_wall_of_text():
    out = svc._transcript("p1", [{"role": "user", "content": "x" * 50_000}], "")
    assert len(out) < svc.MAX_CHARS + 100


def test_only_the_newest_turn_re_reads_its_files(monkeypatch):
    """Re-inlining every attachment on every turn multiplies the bill by the chat's length."""
    monkeypatch.setattr(svc.s3, "get_blob", lambda key: b"the file body")
    old = {"role": "user", "content": "look", "attachments": [{"id": "a" * 32, "name": "old.txt"}]}
    new = {"role": "user", "content": "now this", "attachments": [{"id": "b" * 32, "name": "new.txt"}]}
    out = svc._transcript("p1", [old, {"role": "assistant", "content": "ok"}, new], "")

    assert out.count("the file body") == 1  # the newest one only
    assert "--- new.txt ---" in out
    assert "earlier attachments: old.txt" in out  # named, so the model knows it existed


def test_an_unreadable_attachment_is_still_announced():
    out = svc._transcript(
        "p1", [{"role": "user", "content": "read this", "attachments":
                [{"id": "c" * 32, "name": "report.pdf", "mime": "application/pdf", "size": 12}]}], ""
    )
    assert "report.pdf" in out and "not readable as text" in out


@pytest.mark.parametrize(
    "att,expected",
    [
        ({"name": "a.txt", "mime": "text/plain"}, True),
        ({"name": "trace.json", "mime": "application/json"}, True),
        # the browser guesses octet-stream for most of what a developer actually drags in
        ({"name": "run.log", "mime": "application/octet-stream"}, True),
        ({"name": "shot.png", "mime": "image/png"}, False),
        ({"name": "report.pdf", "mime": "application/pdf"}, False),
    ],
)
def test_what_counts_as_readable_text(att, expected):
    assert svc._is_text(att) is expected


def test_an_image_rides_along_as_a_content_block(monkeypatch):
    monkeypatch.setattr(svc.s3, "get_blob", lambda key: b"\x89PNG fake")
    blocks = svc._image_blocks("p1", [{"id": "d" * 32, "mime": "image/png", "name": "s.png"}])
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_an_image_too_big_to_be_worth_sending_is_skipped(monkeypatch):
    monkeypatch.setattr(svc.s3, "get_blob", lambda key: b"x" * (svc.MAX_IMAGE_BYTES + 1))
    assert svc._image_blocks("p1", [{"id": "e" * 32, "mime": "image/png"}]) == []


def test_title_is_the_opening_question_cut_at_a_word():
    assert svc.title_for("  why did   my gate fail? ") == "why did my gate fail?"
    long = svc.title_for("word " * 40)
    assert len(long) <= 60 and long.endswith("…") and not long.endswith(" …")
    assert svc.title_for("") == "New conversation"


def test_the_prompt_names_the_surfaces_the_assistant_can_send_people_to():
    """A capability the prompt never mentions is a capability nobody discovers. The assistant is
    the main way people find out a screen exists, so shipping one without naming it here is
    shipping it half-hidden."""
    for phrase in (
        "Alerts",              # the page
        "/settings/alerts",    # the link it should hand over
        "draft_alert",         # how it draws a multi-step flow onto that page's canvas
        "create_alert",        # what it can do itself, without sending them anywhere
        "Run test",            # how they check an alert reaches them
        "/clusters/",
        "/scenarios",
    ):
        assert phrase in svc.SYSTEM, f"the system prompt never mentions {phrase!r}"


# ---------------------------------------------------------------- whose key


async def test_no_llm_key_is_a_state_not_a_crash(db, monkeypatch):
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: False)
    assert await turn("p1", "u1", chat_id=None, message="hi") == {"type": "disabled"}


async def test_reply_is_the_model_text_on_the_configured_model(db, model):
    out = await turn("p1", "u1", chat_id=None, message="what is a gate?")

    assert out["reply"] == "hello"
    assert "what is a gate?" in model["prompt"]
    assert "Tracely" in model["system_prompt"]
    assert model["model"] == svc.settings.assistant_model
    assert model["reasoning_effort"] == svc.settings.assistant_reasoning_effort


async def test_the_agent_gets_the_callers_own_credentials(db, model):
    """We pay for the tokens; the TOOLS still run as the person chatting. Handing them the server
    key — or nothing — would either widen their reach or silently blind the agent."""
    await turn("p1", "u1", chat_id=None, message="hi", headers={"authorization": "Bearer theirs"})

    names = {t.name for t in model["tools"]}
    assert {"get_trace", "create_evaluator", "promote_cluster"} <= names
    # every tool closes over the caller's header, not ours
    assert svc.assistant_tools.build_tools.__module__ == "tracely.services.assistant_tools"


async def test_the_assistant_never_spends_the_customers_key(db, monkeypatch):
    """The widget explains OUR product; it must not bill — or depend on — a workspace's key."""
    monkeypatch.setattr(
        svc.provider,
        "use_project_key",
        lambda _p: pytest.fail("the assistant must not use the customer's key"),
    )
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda prompt, **kw: _stream({"type": "final", "text": "ok", "usage": {}}),
    )
    monkeypatch.setattr(provider.settings, "openrouter_api_key", "sk-ours")
    # a workspace with no key of its own still gets an answer, on the server key
    monkeypatch.setattr(provider, "_encrypted_key_for", lambda _p: None)
    out = await turn("p1", "u1", chat_id=None, message="hi")
    assert out["reply"] == "ok"


def test_server_scope_survives_the_hosted_bring_your_own_key_gate(monkeypatch):
    """REQUIRE_PROJECT_LLM_KEY makes an *unscoped* call fail closed — that guard catches paths
    that forgot `use_project_key`, and must not catch the one call we mean to pay for."""
    monkeypatch.setattr(provider.settings, "require_project_llm_key", True)
    monkeypatch.setattr(provider.settings, "openrouter_api_key", "sk-ours")

    assert provider.llm_enabled() is False  # unscoped: nothing server-wide applies
    with provider.use_server_key():
        assert provider.llm_enabled() is True
        assert provider.effective_openrouter_key() == "sk-ours"
    assert provider.llm_enabled() is False  # and the scope is restored on exit


# ---------------------------------------------------------------- what is stored


async def test_a_conversation_accumulates_across_turns(db, model):
    first = await turn("p1", "u1", chat_id=None, message="one")
    second = await turn("p1", "u1", chat_id=first["chat_id"], message="two")

    assert second["chat_id"] == first["chat_id"]  # same conversation, not a new one per turn
    assert second["title"] == "one"  # named by its opening question, and it stays named that
    with svc.SyncSessionLocal() as s:
        stored = svc.repo.assistant_chat_get(s, "p1", "u1", first["chat_id"]).messages
    assert [m["role"] for m in stored] == ["user", "assistant", "user", "assistant"]
    assert "one" in model["prompt"] and "two" in model["prompt"]  # the model saw the whole thread


async def test_a_failed_turn_is_not_stored(db, monkeypatch):
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)

    async def explodes():
        raise RuntimeError("402")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(svc.provider, "stream_agent", lambda *a, **k: explodes())
    with pytest.raises(RuntimeError):
        await turn("p1", "u1", chat_id=None, message="hi")
    with svc.SyncSessionLocal() as s:
        assert svc.repo.assistant_chat_list(s, "p1", "u1") == []


async def test_a_turn_that_worked_but_said_nothing_is_a_failure(db, monkeypatch):
    """An agent that ran tools and then produced no text has done the work and told the user
    nothing. Storing that leaves a blank bubble in history for ever."""
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda *a, **k: _stream(
            {"type": "tool", "name": "list_traces", "args": {}},
            {"type": "final", "text": "   ", "usage": {}},
        ),
    )
    with pytest.raises(RuntimeError):
        await turn("p1", "u1", chat_id=None, message="hi")
    with svc.SyncSessionLocal() as s:
        assert svc.repo.assistant_chat_list(s, "p1", "u1") == []


async def test_tool_activity_reaches_the_caller_but_not_the_stored_transcript(db, monkeypatch):
    """The widget needs the tool frames live; history should stay the conversation the human had."""
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda *a, **k: _stream(
            {"type": "tool", "name": "get_trace", "args": {"trace_id": "t1"}},
            {"type": "tool_done", "name": "get_trace", "ok": True},
            {"type": "delta", "text": "it "},
            {"type": "delta", "text": "failed"},
            {"type": "final", "text": "it failed", "usage": {}},
        ),
    )
    frames = [f async for f in svc.answer_stream("p1", "u1", chat_id=None, message="why?")]

    assert [f["type"] for f in frames] == ["tool", "tool_done", "delta", "delta", "done"]
    with svc.SyncSessionLocal() as s:
        stored = svc.repo.assistant_chat_get(s, "p1", "u1", frames[-1]["chat_id"]).messages
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[-1]["content"] == "it failed"  # the answer, not the tool traffic behind it


async def test_one_persons_chat_is_not_anothers(db, model):
    mine = await turn("p1", "u1", chat_id=None, message="mine")

    # a guessed id belonging to someone else must not read OR overwrite their conversation
    theirs = await turn("p1", "u2", chat_id=mine["chat_id"], message="theirs")
    assert theirs["chat_id"] != mine["chat_id"]

    with svc.SyncSessionLocal() as s:
        assert [c.id for c in svc.repo.assistant_chat_list(s, "p1", "u1")] == [mine["chat_id"]]
        assert [c.id for c in svc.repo.assistant_chat_list(s, "p1", "u2")] == [theirs["chat_id"]]


async def test_the_endpoint_streams_frames_and_terminates(client, make_workspace, monkeypatch):
    """The wire format the widget decodes: `data: <json>` lines, `[DONE]` last. A turn that dies
    mid-stream is an `error` FRAME, not a 502 — the status was already 200 by then."""
    from tracely.api.routers import assistant as router

    monkeypatch.setattr(
        router.assistant_service, "answer_stream",
        lambda *a, **k: _stream(
            {"type": "tool", "name": "list_traces", "args": {}},
            {"type": "done", "chat_id": "c1", "title": "t", "reply": "hi"},
        ),
    )
    await make_workspace("sse", "sse_key", "sse@x.test")
    r = await client.post(
        "/api/assistant/chat",
        json={"message": "hi"},
        headers={"Authorization": "Bearer sse_key"},
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert '"type": "tool"' in frames[0]
    assert '"reply": "hi"' in frames[1]
    assert frames[-1] == "[DONE]"


async def test_a_dying_turn_is_a_frame_not_a_500(client, make_workspace, monkeypatch):
    from tracely.api.routers import assistant as router

    async def explodes(*a, **k):
        raise RuntimeError("no credit")
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(router.assistant_service, "answer_stream", explodes)
    await make_workspace("sse-err", "sse_err_key", "sseerr@x.test")
    r = await client.post(
        "/api/assistant/chat",
        json={"message": "hi"},
        headers={"Authorization": "Bearer sse_err_key"},
    )

    assert r.status_code == 200
    assert '"type": "error"' in r.text and "no credit" in r.text
    assert r.text.rstrip().endswith("[DONE]")


async def test_signed_out_callers_share_the_projects_chats(db, model):
    """An ingest key (and dev mode) has no human identity — `user_id IS NULL`, which SQL will
    never match with `= NULL`, so this is the case a mocked repository would fake passing."""
    made = await turn("p1", None, chat_id=None, message="hi")
    with svc.SyncSessionLocal() as s:
        assert [c.id for c in svc.repo.assistant_chat_list(s, "p1", None)] == [made["chat_id"]]
        assert svc.repo.assistant_chat_get(s, "p1", None, made["chat_id"]) is not None


# ---------------------------------------------------------------- what it costs us


def test_a_normal_turn_is_invisible_to_integer_cents():
    """Why the budget is float dollars. A turn costs about half a cent, and the cents estimator
    the scores use rounds that to 0 — accumulate it and a cap never fires, however long it runs."""
    assert provider.estimate_cost_usd_cents("google/gemini-3.7-flash", 3_000, 300) == 0
    assert provider.estimate_cost_usd("google/gemini-3.7-flash", 3_000, 300) > 0.003


def test_spend_accrues_over_a_conversations_assistant_turns():
    history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a", "cost_usd": 0.004},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "b", "cost_usd": 0.006},
    ]
    assert svc.spent_usd(history) == pytest.approx(0.01)
    assert svc.spent_usd([{"role": "assistant", "content": "old"}]) == 0.0  # pre-budget turns


async def test_a_turn_records_what_it_cost(db, model, monkeypatch):
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda prompt, **kw: _stream({
            "type": "final", "text": "hi", "usage": {
                "model": "google/gemini-3.7-flash", "input_tokens": 10_000, "output_tokens": 550,
            },
        }),
    )
    out = await turn("p1", "u1", chat_id=None, message="hi")
    with svc.SyncSessionLocal() as s:
        stored = svc.repo.assistant_chat_get(s, "p1", "u1", out["chat_id"]).messages
    assert stored[-1]["cost_usd"] == pytest.approx(0.00956, abs=1e-4)


async def test_a_conversation_that_spent_its_budget_is_refused_before_the_model(db, monkeypatch):
    """The cheapest turn is the one never sent. Refusing must not cost a model call."""
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda *a, **k: pytest.fail("an over-budget conversation must not reach the model"),
    )
    monkeypatch.setattr(svc.settings, "assistant_budget_usd", 0.01)

    with svc.SyncSessionLocal() as s:
        saved = svc.repo.assistant_chat_save(
            s, "p1", "u1", chat_id=None, title="spendy",
            messages=[{"role": "assistant", "content": "prior", "cost_usd": 0.02}],
        )
        chat_id = saved.id

    out = await turn("p1", "u1", chat_id=chat_id, message="more please")
    assert out["type"] == "over_budget"
    assert out["budget_usd"] == 0.01 and out["spent_usd"] == pytest.approx(0.02)


async def test_the_turns_remaining_budget_is_what_reaches_the_loop(db, model, monkeypatch):
    """A single runaway turn has to be stoppable too, so the loop gets what's LEFT, not the whole
    allowance — otherwise turn two of a nearly-spent chat could spend the budget over again."""
    monkeypatch.setattr(svc.settings, "assistant_budget_usd", 1.0)
    with svc.SyncSessionLocal() as s:
        chat_id = svc.repo.assistant_chat_save(
            s, "p1", "u1", chat_id=None, title="t",
            messages=[{"role": "assistant", "content": "prior", "cost_usd": 0.25}],
        ).id

    await turn("p1", "u1", chat_id=chat_id, message="again")
    assert model["budget_usd"] == pytest.approx(0.75)


async def test_a_budget_stop_mid_loop_is_an_answer_not_an_error(db, monkeypatch):
    """It already did the work and charged us; ending the turn with a raise would throw that away
    and tell the user nothing about why."""
    monkeypatch.setattr(svc.provider, "use_server_key", contextlib.nullcontext)
    monkeypatch.setattr(svc.provider, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        svc.provider, "stream_agent",
        lambda *a, **k: _stream(
            {"type": "tool", "name": "list_traces", "args": {}},
            {"type": "final", "text": "", "usage": {}, "stopped": "budget"},
        ),
    )
    out = await turn("p1", "u1", chat_id=None, message="loop forever")
    assert out["type"] == "done"
    assert "spend limit" in out["reply"]


# ---------------------------------------------------------------- the tool picker


def _fake_chat_model():
    """The selector validates its model is a real `BaseChatModel` (a string would send it off to
    langchain's own provider resolution — off OpenRouter, off our key), so the stub must be one."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    return GenericFakeChatModel(messages=iter([]))


def test_selection_is_optional_and_the_loop_cap_is_not(monkeypatch):
    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: _fake_chat_model())

    both = provider.agent_middleware(selector_model="x/y", max_tools=8, max_model_calls=12)
    assert len(both) == 2

    # ASSISTANT_MAX_TOOLS=0 hands the model every tool again — the escape hatch if a picker
    # ever picks badly. The runaway cap stays either way.
    off = provider.agent_middleware(selector_model="x/y", max_tools=0, max_model_calls=12)
    assert len(off) == 1


def test_the_picker_runs_on_our_key_not_langchains_own_provider_resolution(monkeypatch):
    """Passing a model *string* would let langchain build its own client — off OpenRouter, off
    our key, outside `provider`. The whole point of the gateway is that this can't happen."""
    asked: list = []
    monkeypatch.setattr(
        provider, "get_chat_model", lambda m, *a, **k: asked.append(m) or _fake_chat_model()
    )
    provider.agent_middleware(selector_model="openai/gpt-oss-120b", max_tools=4)
    assert asked == ["openai/gpt-oss-120b"]


async def test_the_tool_picker_decides_once_per_turn(monkeypatch):
    """Selection reads the last user message, which cannot change inside a turn — so re-running
    it per model call pays for an extra model call to be told the same thing."""
    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: _fake_chat_model())
    selector = provider.agent_middleware(selector_model="x/y", max_tools=3)[0]
    selector._picked = ["already-chosen"]  # as if an earlier model call had selected

    overridden: dict = {}

    class Request:
        def override(self, **kw):
            overridden.update(kw)
            return "reused"

    async def handler(request):
        return request

    assert await selector.awrap_model_call(Request(), handler) == "reused"
    assert overridden["tools"] == ["already-chosen"]


async def test_only_the_answering_models_tokens_are_streamed(monkeypatch):
    """The tool picker calls its own model INSIDE the agent graph, so its tokens arrive on the
    same stream from the same node. Unfiltered, the user watches the picker's raw JSON get typed
    into the chat before the answer — which is exactly what shipped until this test existed."""
    from langchain_core.messages import AIMessage, AIMessageChunk

    def frames():
        picker = AIMessageChunk(content='{"tools": ["get_trace"]}')
        answer = AIMessageChunk(content="it failed because of a timeout")
        return [
            ("messages", (picker, {"langgraph_node": "model", "ls_model_name": "picker/cheap"})),
            ("messages", (answer, {"langgraph_node": "model", "ls_model_name": "answer/model"})),
            ("updates", {"model": {"messages": [AIMessage(content="it failed because of a timeout")]}}),
        ]

    class FakeAgent:
        def astream(self, _input, **kw):
            async def gen():
                for f in frames():
                    yield f
            return gen()

    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: object())
    monkeypatch.setattr(
        "langchain.agents.create_agent", lambda *a, **k: FakeAgent(), raising=False
    )
    events = [
        e async for e in provider.stream_agent("q", tools=[], model="answer/model")
    ]
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["it failed because of a timeout"]
    assert not any("tools" in d for d in deltas)


def test_the_picker_declines_to_pick_with_the_model_it_picks_for(monkeypatch):
    """No saving, and no way to separate the two on the stream — so it must not be configured on."""
    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: _fake_chat_model())
    same = provider.agent_middleware(
        selector_model="google/gemini-3.7-flash",
        answering_model="google/gemini-3.7-flash",
        max_tools=8,
        max_model_calls=12,
    )
    assert len(same) == 1  # the loop cap only


async def test_a_failed_tool_pick_falls_back_to_every_tool(monkeypatch):
    """The picker raises on a hallucinated tool name — observed live, with the real
    `list_clusters` guessed as `list_failure_clusters`. It is an optimization, so it must fail
    OPEN: killing a turn to save a few thousand tokens is a terrible trade."""
    monkeypatch.setattr(provider, "get_chat_model", lambda *a, **k: _fake_chat_model())
    selector = provider.agent_middleware(selector_model="x/y", max_tools=3)[0]

    class Request:
        tools = ["all", "the", "tools"]

        def override(self, **kw):  # pragma: no cover — the fallback must not go through this
            raise AssertionError("the fallback should hand back the untouched request")

    async def handler(request):
        return request

    # the upstream selector blows up the way it does on an invalid selection
    async def boom(request, handler):
        raise ValueError("Model selected invalid tools: ['list_failure_clusters']")

    monkeypatch.setattr(type(selector).__mro__[1], "awrap_model_call", boom)

    request = Request()
    assert await selector.awrap_model_call(request, handler) is request


async def test_a_silent_turn_still_says_it_is_alive(client, make_workspace, monkeypatch):
    """A turn that runs a scenario or a backfill emits nothing for minutes. Proxies close a stream
    that quiet, and the browser shows a chat that stopped mid-answer — so the stream has to keep
    talking. An SSE comment is the protocol's own way to do it; parsers drop it."""
    import asyncio

    from tracely.api.routers import assistant as router

    monkeypatch.setattr(router, "PING_EVERY_S", 0.01)

    async def slow(*a, **k):
        await asyncio.sleep(0.05)  # several ping intervals of silence
        yield {"type": "done", "chat_id": "c1", "title": "t", "reply": "took a while"}

    monkeypatch.setattr(router.assistant_service, "answer_stream", slow)
    await make_workspace("ping", "ping_key", "ping@x.test")
    r = await client.post(
        "/api/assistant/chat", json={"message": "run it"},
        headers={"Authorization": "Bearer ping_key"},
    )

    assert ": keep-alive" in r.text
    assert '"reply": "took a while"' in r.text  # the answer still arrives, after the pings
    assert r.text.rstrip().endswith("[DONE]")
    # a comment is not a frame: the browser's reader only looks at `data: ` lines
    assert not any(ln.startswith("data: ") and "keep-alive" in ln for ln in r.text.splitlines())


async def test_a_client_that_leaves_stops_the_agent(monkeypatch):
    """Closing the panel mid-turn must stop the WORK, not just stop watching it — an abandoned
    tool loop keeps calling tools and spending on an answer nobody will ever read. Starlette
    closes the response generator on disconnect; this checks that closing it reaches the agent."""
    import asyncio

    from tracely.api.routers import assistant as router
    from tracely.auth import Principal

    stopped = asyncio.Event()

    async def forever(*a, **k):
        try:
            while True:
                yield {"type": "delta", "text": "..."}
                await asyncio.sleep(0.01)
        finally:
            stopped.set()

    monkeypatch.setattr(router.assistant_service, "answer_stream", forever)

    class FakeRequest:
        headers: dict = {}

    response = await router.chat(
        router.ChatBody(message="go"),
        FakeRequest(),
        Principal(project_id="p1", user_id="u1", role=None, kind="ingest"),
    )
    body = response.body_iterator
    assert "delta" in await body.__anext__()  # streaming has started
    await body.aclose()  # the client goes away

    assert stopped.is_set()


async def test_the_router_drains_a_turn_in_a_single_context(client, make_workspace, monkeypatch):
    """A turn sets contextvars that outlive individual yields — the LLM key scope, and the
    introspection recording whose token is reset at the end. Draining the stream with a task per
    `__anext__` runs each step in a COPY of the context, so that reset raises "created in a
    different Context" and the turn dies right after answering. Live-only bug until this test.
    """
    import contextvars

    from tracely.api.routers import assistant as router

    probe: contextvars.ContextVar = contextvars.ContextVar("probe", default="")

    async def turn(*a, **k):
        token = probe.set("open")
        try:
            yield {"type": "delta", "text": "hi"}
            yield {"type": "done", "chat_id": "c1", "title": "t", "reply": "hi"}
        finally:
            probe.reset(token)  # the line that used to explode

    monkeypatch.setattr(router.assistant_service, "answer_stream", turn)
    await make_workspace("ctx", "ctx_key", "ctx@x.test")
    r = await client.post(
        "/api/assistant/chat", json={"message": "hi"},
        headers={"Authorization": "Bearer ctx_key"},
    )

    assert '"type": "error"' not in r.text, r.text
    assert '"reply": "hi"' in r.text
    assert r.text.rstrip().endswith("[DONE]")
