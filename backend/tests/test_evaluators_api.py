"""Evaluator (= evaluation column) management API: CRUD + templates + generate.

The evaluators router does its Postgres work through the SYNC `SyncSessionLocal` (same pattern
as cases/clusters), so this module overrides the conftest `engine` with a file-backed SQLite db
that a sync engine can share, and points the router's sessionmaker at it.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base

_TABLES = [
    models.Project.__table__,
    models.IngestKey.__table__,
    models.User.__table__,
    models.Membership.__table__,
    models.Organization.__table__,
    models.OrgMembership.__table__,
    models.Invitation.__table__,
    models.Evaluator.__table__,
    models.EvalChainProgress.__table__,
]


@pytest_asyncio.fixture
async def engine(tmp_path):
    """File-backed SQLite (overrides conftest's :memory: engine for this module) so the sync
    sessionmaker used by the evaluators router can see the same database."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    sync_eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    maker = sessionmaker(sync_eng)
    import tracely.api.routers.evaluators as evaluators_router

    monkeypatch.setattr(evaluators_router, "SyncSessionLocal", maker)
    yield maker
    sync_eng.dispose()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _workspace_key(monkeypatch, key: str = "test-key") -> None:
    """Give the request's project its own OpenRouter key. Server-wide keys don't apply inside a
    project scope any more (customers pay their own eval spend), so LLM-backed endpoints need
    this rather than `settings.openrouter_api_key`."""
    from tracely.infrastructure.llm import provider

    monkeypatch.setattr(provider, "_encrypted_key_for", lambda pid: "enc")
    monkeypatch.setattr(provider, "_decrypt_project_key", lambda tok: key)


async def _owner_token(client) -> str:
    r = await client.post(
        "/auth/register", json={"email": "owner@x.test", "password": "hunter2-pw"}
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_workspace_bootstrap_seeds_recommended_evaluators(client, sync_db):
    tok = await _owner_token(client)
    r = await client.get("/api/evaluators", headers=_bearer(tok))
    assert r.status_code == 200, r.text
    names = {e["score_name"] for e in r.json()}
    # the five recommended checks install on workspace bootstrap…
    assert {"tracely.run.outcome", "tracely.tool.success", "tracely.run.quality"} <= names
    # …library-only metrics don't
    assert "tracely.conv.goal_success" not in names


async def test_create_update_delete_evaluator(client, sync_db):
    tok = await _owner_token(client)
    created = await client.post(
        "/api/evaluators",
        headers=_bearer(tok),
        json={
            "name": "Politeness Check",
            "level": "AGENT_RUN",
            "config": {"prompt": "Grade politeness.", "output_type": "score", "threshold": 0.5},
        },
    )
    assert created.status_code == 200, created.text
    e = created.json()
    assert e["kind"] == "llm_judge"
    assert e["score_name"] == "custom.politeness_check"
    assert e["config"]["threshold"] == 0.5

    # same name again → score_name gets a suffix instead of colliding
    dup = await client.post(
        "/api/evaluators", headers=_bearer(tok), json={"name": "Politeness Check"}
    )
    assert dup.json()["score_name"] == "custom.politeness_check_2"

    patched = await client.patch(
        f"/api/evaluators/{e['id']}",
        headers=_bearer(tok),
        json={"enabled": False, "config": {"prompt": "Stricter.", "output_type": "boolean"}},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["config"]["output_type"] == "boolean"

    deleted = await client.delete(f"/api/evaluators/{e['id']}", headers=_bearer(tok))
    assert deleted.status_code == 200
    again = await client.delete(f"/api/evaluators/{e['id']}", headers=_bearer(tok))
    assert again.status_code == 404


async def test_create_rejects_bad_level_and_kind(client, sync_db):
    tok = await _owner_token(client)
    bad_level = await client.post(
        "/api/evaluators", headers=_bearer(tok), json={"name": "x", "level": "BANANAS"}
    )
    assert bad_level.status_code == 400
    bad_kind = await client.post(
        "/api/evaluators", headers=_bearer(tok), json={"name": "x", "kind": "python"}
    )
    assert bad_kind.status_code == 400


async def test_structural_checks_reject_a_level_they_cannot_address(client, sync_db):
    """A run-level structural check stamped as TOOL has no tool observation id, so it used to
    persist an invisible score. Reject the invalid pairing before it enters the runner."""
    tok = await _owner_token(client)
    bad = await client.post(
        "/api/evaluators",
        headers=_bearer(tok),
        json={
            "name": "Bad latency column",
            "kind": "structural",
            "level": "TOOL",
            "config": {"check": "latency"},
        },
    )
    assert bad.status_code == 400
    assert "must use level AGENT_RUN" in bad.json()["detail"]


async def test_judge_rejects_unknown_execution_mode_and_output_type(client, sync_db):
    tok = await _owner_token(client)
    bad_mode = await client.post(
        "/api/evaluators",
        headers=_bearer(tok),
        json={"name": "Bad mode", "config": {"execution_mode": "parallel"}},
    )
    assert bad_mode.status_code == 400
    bad_type = await client.post(
        "/api/evaluators",
        headers=_bearer(tok),
        json={"name": "Bad type", "config": {"output_type": "matrix"}},
    )
    assert bad_type.status_code == 400


async def test_templates_listing_marks_installed(client, sync_db):
    tok = await _owner_token(client)
    r = await client.get("/api/evaluators/templates", headers=_bearer(tok))
    assert r.status_code == 200
    by_name = {t["score_name"]: t for t in r.json()}
    assert by_name["tracely.run.outcome"]["installed"] is True
    goal = by_name["tracely.conv.goal_success"]
    assert goal["installed"] is False
    assert goal["level"] == "CONVERSATION"
    assert goal["kind"] == "llm_judge"
    # step-level library entry rides on the SPAN level
    assert by_name["tracely.step.tool_choice"]["level"] == "SPAN"


async def test_evaluators_are_project_scoped(client, sync_db, make_workspace):
    tok = await _owner_token(client)
    # a second, separate workspace authed via its ingest key
    await make_workspace("other", "tk_other_key", "other@x.test")
    r = await client.post(
        "/api/evaluators", headers=_bearer("tk_other_key"), json={"name": "Other Metric"}
    )
    assert r.status_code == 200, r.text
    other_id = r.json()["id"]

    mine = await client.get("/api/evaluators", headers=_bearer(tok))
    assert other_id not in {e["id"] for e in mine.json()}

    # cross-project mutation is a 404
    stolen = await client.delete(f"/api/evaluators/{other_id}", headers=_bearer(tok))
    assert stolen.status_code == 404


async def test_models_endpoint_static_fallback(client, sync_db, monkeypatch):
    from tracely.infrastructure.llm import provider

    _workspace_key(monkeypatch)
    monkeypatch.setattr(provider, "_openrouter_model_names", lambda: {})
    tok = await _owner_token(client)
    r = await client.get("/api/evaluators/models", headers=_bearer(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    # a new column starts on the decision model; text work keeps a chat model
    assert body["default"] == "typesafe/jev-1.13"
    assert body["text_default"] == "openai/gpt-6-luna"
    ids = [m["id"] for m in body["models"]]
    assert "openai/gpt-6-luna" in ids and "openai/gpt-5.4-nano" not in ids
    assert len(ids) >= 8 and all(m["label"] for m in body["models"])


async def test_models_endpoint_legacy_key_offers_only_openai(client, sync_db, monkeypatch):
    """With only the legacy direct endpoint configured, non-openai OpenRouter ids can't be
    served — the selector narrows instead of offering models that would always fail."""
    from tracely.config import settings

    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_judge_api_key", "legacy-key")
    tok = await _owner_token(client)
    r = await client.get("/api/evaluators/models", headers=_bearer(tok))
    ids = [m["id"] for m in r.json()["models"]]
    assert ids and all(i.startswith("openai/") for i in ids)


async def test_models_endpoint_filters_to_available(client, sync_db, monkeypatch):
    from tracely.infrastructure.llm import provider

    monkeypatch.setattr(
        provider, "_openrouter_model_names",
        lambda: {"openai/gpt-6-luna": "OpenAI: GPT-6 Luna"},
    )
    tok = await _owner_token(client)
    r = await client.get("/api/evaluators/models", headers=_bearer(tok))
    assert r.json()["models"] == [
        {"id": "openai/gpt-6-luna", "label": "OpenAI: GPT-6 Luna", "kind": "llm", "context_tokens": None}
    ]
    # no workspace key → no Decisions API → a new column starts on the text default
    assert r.json()["default"] == "openai/gpt-6-luna"


async def test_generate_json_draft_builds_schema(client, sync_db, monkeypatch):
    from tracely.domain.evaluation.generation import GeneratedEvaluatorDraft, GeneratedSchemaField
    from tracely.infrastructure.llm import provider

    _workspace_key(monkeypatch)
    monkeypatch.setattr(
        provider, "run_structured_agent",
        lambda prompt, *, response_format, system_prompt=None, model=None, temperature=0.0:
            GeneratedEvaluatorDraft(
                name="Intent classifier",
                description="Classifies the user's intent.",
                level="AGENT_RUN",
                output_type="json",
                prompt="Classify the intent.",
                schema_fields=[
                    GeneratedSchemaField(name="intent", type="enum", enum_values=["a", "b"], required=True),
                    GeneratedSchemaField(name="bad name!", type="string"),  # dropped: not an identifier
                    GeneratedSchemaField(name="reasoning", type="string", required=True),
                ],
            ),
    )
    tok = await _owner_token(client)
    r = await client.post(
        "/api/evaluators/generate", headers=_bearer(tok), json={"description": "classify intent"}
    )
    assert r.status_code == 200, r.text
    schema = r.json()["config"]["output_schema"]
    assert schema["properties"]["intent"]["enum"] == ["a", "b"]
    assert "bad name!" not in schema["properties"]  # dropped: not an identifier
    # nothing is reserved anymore — the user/AI defines every field, including reasoning
    assert "reasoning" in schema["properties"]
    assert schema["required"] == ["intent", "reasoning"]


async def test_generate_without_llm_key_is_503(client, sync_db, monkeypatch):
    from tracely.config import settings

    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_judge_api_key", "")
    tok = await _owner_token(client)
    r = await client.post(
        "/api/evaluators/generate", headers=_bearer(tok), json={"description": "politeness"}
    )
    assert r.status_code == 503


async def test_generate_returns_normalized_draft(client, sync_db, monkeypatch):
    from tracely.domain.evaluation.generation import GeneratedEvaluatorDraft
    from tracely.infrastructure.llm import provider

    _workspace_key(monkeypatch)
    monkeypatch.setattr(
        provider,
        "run_structured_agent",
        lambda prompt, *, response_format, system_prompt=None, model=None, temperature=0.0:
            GeneratedEvaluatorDraft(
                name="Politeness",
                description="Checks politeness.",
                level="agent_run",  # case-normalized
                output_type="SCORE",
                prompt="Grade the reply's politeness.",
                threshold=0.7,
            ),
    )
    tok = await _owner_token(client)
    r = await client.post(
        "/api/evaluators/generate", headers=_bearer(tok), json={"description": "politeness"}
    )
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["kind"] == "llm_judge"
    assert draft["level"] == "AGENT_RUN"
    assert draft["config"]["output_type"] == "score"
    assert draft["config"]["threshold"] == 0.7
    assert draft["config"]["prompt"].startswith("Grade")


async def test_create_rejects_malformed_config_knobs(client, sync_db):
    """Knobs the runner would trip over mid-grade fail at save time instead — a bad value there
    doesn't crash the pipeline, it makes the column silently stop producing scores."""
    tok = await _owner_token(client)

    async def rejected(config: dict) -> str:
        r = await client.post(
            "/api/evaluators", headers=_bearer(tok),
            json={"name": "x", "kind": "llm_judge", "level": "AGENT_RUN", "config": config},
        )
        assert r.status_code == 400, config
        return r.json()["detail"]

    assert "threshold" in await rejected({"threshold": "0.6 or so"})
    assert "max_spans" in await rejected({"max_spans": 0})
    assert "span_types" in await rejected({"span_types": ["TOOL", "BANANA"]})
    assert "depends_on" in await rejected({"depends_on": "helpfulness"})
    # a schema the compiler can't build a contract from would silently fall back to free-form
    assert "output_schema" in await rejected(
        {"output_type": "json", "output_schema": {"type": "object", "properties": {}}}
    )
    # the same knobs well-formed are accepted
    ok = await client.post(
        "/api/evaluators", headers=_bearer(tok),
        json={"name": "ok", "kind": "llm_judge", "level": "TOOL", "config": {
            "threshold": 0.6, "max_spans": 10, "span_types": ["TOOL"],
            "depends_on": ["tracely.tool.success"], "output_type": "json",
            "output_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
        }},
    )
    assert ok.status_code == 200


async def test_chain_progress_reports_sequential_columns(client, sync_db, monkeypatch):
    """One entry per enabled sequential message/step column: turns chained vs the thread's turn
    count, the next seed payload, freshness. Batch columns don't chain, so they don't appear."""
    from sqlalchemy import select

    from tracely.infrastructure.clickhouse import async_reader
    from tracely.infrastructure.db import repositories as repo
    from tracely.infrastructure.db.models import Evaluator

    tok = await _owner_token(client)

    async def fake_count(project_id, thread_id):
        return 3

    monkeypatch.setattr(async_reader, "thread_turn_count", fake_count)
    import tracely.api.routers.sessions as sessions_router

    monkeypatch.setattr(sessions_router, "SyncSessionLocal", sync_db)
    r = await client.post(
        "/api/evaluators", headers=_bearer(tok),
        json={"name": "Helpfulness", "kind": "llm_judge", "level": "AGENT_RUN",
              "score_name": "helpfulness",
              "config": {"execution_mode": "sequential", "threshold": 0.6}},
    )
    assert r.status_code == 200
    with sync_db() as s:
        ev = s.execute(select(Evaluator).where(Evaluator.score_name == "helpfulness")).scalar_one()
        repo.chain_progress_set(
            s, ev.project_id, "helpfulness", "th-1", ["t1", "t2"],
            {"verdict": "PASS", "value": 0.9},
        )

    r = await client.get("/api/sessions/th-1/chain-progress", headers=_bearer(tok))
    assert r.status_code == 200
    metrics = {m["score_name"]: m for m in r.json()["metrics"]}
    # the bootstrap's batch columns don't chain; its sequential intent column does, alongside ours
    assert set(metrics) == {"helpfulness", "tracely.run.intent"}
    m = metrics["helpfulness"]
    assert (m["chained"], m["turns"], m["up_to_date"]) == (2, 3, False)
    assert m["last_payload"] == {"verdict": "PASS", "value": 0.9}
    assert isinstance(m["updated_at"], str)

    # a thread with no progress rows still lists the column, at zero
    r = await client.get("/api/sessions/th-other/chain-progress", headers=_bearer(tok))
    m = next(m for m in r.json()["metrics"] if m["score_name"] == "helpfulness")
    assert (m["chained"], m["up_to_date"], m["last_payload"]) == (0, False, None)


# ── decision models (TypeSafe Jev) ───────────────────────────────────────────

_JEV_NOUL = {
    "model": "typesafe/jev-1.13", "output_type": "decision_binary",
    "question": "Did the agent complete the task?",
    "criteria": {"true": "Done", "false": "Not done"},
}


async def test_decision_column_roundtrips(client, sync_db):
    tok = await _owner_token(client)
    r = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "Task done (Jev)", "level": "AGENT_RUN",
        "config": {**_JEV_NOUL, "fallback_model": "google/gemini-3.6-flash"},
    })
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["output_type"] == "decision_binary" and cfg["fallback_model"] == "google/gemini-3.6-flash"


@pytest.mark.parametrize("config, needle", [
    # a decision model can't grade a rubric
    ({"model": "typesafe/jev-1.13", "output_type": "score", "prompt": "Grade."}, "decision model"),
    # a decision output type needs a decision model
    ({**_JEV_NOUL, "model": "openai/gpt-5.4-nano"}, "needs a decision model"),
    # Jev's own limits, surfaced before any spend
    ({**_JEV_NOUL, "criteria": {"true": "Done"}}, '"false"'),
    ({**_JEV_NOUL, "output_type": "decision_multiclass", "criteria": {"a": None}}, "between 2 and 255"),
    # a fallback has to be able to read more — an LLM, not another classifier
    ({**_JEV_NOUL, "fallback_model": "typesafe/jev-1.13"}, "fallback_model must be an LLM"),
])
async def test_decision_column_validation(client, sync_db, config, needle):
    tok = await _owner_token(client)
    r = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "x", "level": "AGENT_RUN", "config": config,
    })
    assert r.status_code == 400 and needle in r.json()["detail"], r.text


async def test_models_endpoint_lists_jev_as_decision(client, sync_db, monkeypatch):
    from tracely.infrastructure.llm import provider

    _workspace_key(monkeypatch)
    monkeypatch.setattr(provider, "_openrouter_model_names", lambda: {})
    tok = await _owner_token(client)
    models = (await client.get("/api/evaluators/models", headers=_bearer(tok))).json()["models"]
    jev = [m for m in models if m["kind"] == "decision"]
    assert [m["id"] for m in jev] == ["typesafe/jev-1.13"] and jev[0]["context_tokens"] == 32000


async def test_decision_column_without_a_model_uses_the_column_default(client, sync_db):
    tok = await _owner_token(client)
    cfg = {k: v for k, v in _JEV_NOUL.items() if k != "model"}
    r = await client.post("/api/evaluators", headers=_bearer(tok), json={"name": "x", "config": cfg})
    assert r.status_code == 200, r.text


# ── "Use AI" drafts a decision column first ─────────────────────────────────


def _draft(**kw):
    from tracely.domain.evaluation.generation import GeneratedEvaluatorDraft

    return GeneratedEvaluatorDraft(**{"name": "M", "level": "AGENT_RUN", **kw})


def _generate(monkeypatch, draft, *, key=True):
    from tracely.domain.evaluation.generation import generate_evaluator_config
    from tracely.infrastructure.llm import provider

    if key:
        _workspace_key(monkeypatch)
    else:
        monkeypatch.setattr(provider, "_encrypted_key_for", lambda pid: None)
    seen = {}

    def fake(prompt, *, response_format, system_prompt=None, **_):
        seen["system"] = system_prompt
        return draft

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    return generate_evaluator_config("did the agent hallucinate?", "p1"), seen["system"]


def test_generate_prefers_a_decision_column(monkeypatch):
    out, system = _generate(monkeypatch, _draft(
        engine="decision", task="binary", question="Does the `Agent answer` invent facts?",
        yes_means="It states unsupported facts", no_means="Every fact is supported", pass_when="no",
    ))
    assert 'Set engine="decision" unless' in system
    cfg = out["config"]
    assert cfg["model"] == "typesafe/jev-1.13" and cfg["output_type"] == "decision_binary"
    assert cfg["pass_when"] == "no" and cfg["criteria"]["false"] == "Every fact is supported"


def test_generate_normalizes_labels(monkeypatch):
    from tracely.domain.evaluation.generation import GeneratedLabel

    out, _ = _generate(monkeypatch, _draft(
        engine="decision", task="multiclass", question="Which failure?",
        labels=[GeneratedLabel(name="Wrong Answer!", description="false"), GeneratedLabel(name="ok")],
        fail_labels=["wrong answer"],
    ))
    assert out["config"]["criteria"] == {"wrong_answer": "false", "ok": None}
    assert out["config"]["fail_options"] == ["wrong_answer"]


def test_generate_falls_back_to_a_luna_rubric(monkeypatch):
    # an LLM-shaped metric, and a decision draft too broken to use, both land on the text default
    out, _ = _generate(monkeypatch, _draft(engine="llm", output_type="text", prompt="Describe the tone."))
    assert out["config"]["model"] == "openai/gpt-6-luna" and out["config"]["output_type"] == "text"
    out, _ = _generate(monkeypatch, _draft(engine="decision", task="multiclass", question="Q?", labels=[],
                                            prompt="Grade it."))
    assert out["config"]["model"] == "openai/gpt-6-luna" and out["config"]["prompt"] == "Grade it."


def test_generate_without_a_decision_model_only_offers_llm(monkeypatch):
    out, system = _generate(monkeypatch, _draft(engine="llm", prompt="Grade."), key=False)
    assert "no decision model is available" in system


# ── conditional columns (run_if) ─────────────────────────────────────────────


async def test_run_if_joins_depends_on_and_rejects_unknown_columns(client, sync_db):
    tok = await _owner_token(client)
    cond = [{"column": "tracely.run.quality", "field": "label", "op": "in", "values": ["incorrect"]}]
    ok = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "Explain refunds", "config": {"output_type": "text", "prompt": "Explain.", "run_if": cond},
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["config"]["depends_on"] == ["tracely.run.quality"]  # added for you

    bad = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "x", "config": {"output_type": "text", "prompt": "E.", "run_if": [{**cond[0], "column": "nope"}]},
    })
    assert bad.status_code == 400 and "not columns here" in bad.json()["detail"]
    assert "tracely.run.quality" in bad.json()["detail"]  # lists what does exist

    malformed = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "y", "config": {"output_type": "text", "prompt": "E.", "run_if": [{**cond[0], "op": "equals"}]},
    })
    assert malformed.status_code == 400 and "op must be one of" in malformed.json()["detail"]


async def test_dependencies_across_passes_are_rejected(client, sync_db):
    """Conversation vs message columns, and batch vs sequential ones, run in separate passes that
    never share results — such a dependency could never be read, so it's refused at save."""
    tok = await _owner_token(client)
    conv = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "Conv check", "level": "CONVERSATION", "config": {"output_type": "boolean", "prompt": "P"},
    })
    assert conv.status_code == 200, conv.text
    bad = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "Needs conv", "level": "AGENT_RUN",
        "config": {"output_type": "text", "prompt": "E.", "depends_on": [conv.json()["score_name"]]},
    })
    assert bad.status_code == 400 and "pass" in bad.json()["detail"], bad.text
    # tracely.run.intent is sequential; a batch column can't gate on it
    batch = await client.post("/api/evaluators", headers=_bearer(tok), json={
        "name": "Batch", "level": "AGENT_RUN", "config": {"output_type": "text", "prompt": "E.",
        "run_if": [{"column": "tracely.run.intent", "field": "label", "op": "in", "values": ["x"]}]},
    })
    assert batch.status_code == 400 and "sequential" in batch.json()["detail"], batch.text
