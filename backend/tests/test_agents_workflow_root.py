"""OpenAI Agents SDK traces: the empty "Agent workflow" root gets the run's input/output at ingest,
so the turn has a user message and an answer to grade — and says it was derived."""

from __future__ import annotations

import json

from tracely.services import ingestion_service
from tracely.services.ingestion_service import IngestionService

USER = [{"role": "system", "content": "s"}, {"role": "user", "content": "Where is ORD-1042?"}]
FINAL = [{"role": "assistant", "content": "It ships Friday."}]


def _root(**kw):
    return {"trace_id": "t", "span_id": "r", "parent_span_id": "", "name": "Agent workflow",
            "type": "AGENT", "input": None, "output": None,
            "metadata": {"openinference.span.kind": "AGENT"}, **kw}


def _gen(i, inp, out):
    return {"trace_id": "t", "span_id": f"g{i}", "parent_span_id": "r", "type": "GENERATION",
            "start_time": i, "input": json.dumps(inp), "output": json.dumps(out), "metadata": {}}


def test_the_empty_root_takes_the_first_call_input_and_last_call_output(monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_client", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    call = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_order"}}]}]
    events = [_root(), _gen(1, USER, call), _gen(2, USER + call, FINAL)]
    IngestionService._fill_agents_workflow_root("p", events)
    root = events[0]
    assert json.loads(root["input"]) == USER and json.loads(root["output"]) == FINAL
    assert root["metadata"]["tracely.io_source"] == "derived:openai-agents"


def test_other_roots_are_never_touched(monkeypatch):
    monkeypatch.setattr(ingestion_service, "get_client", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    other = _root(name="lg-supervisor")
    recorded = _root(input="hi", output="hello")
    events = [other, recorded, _gen(1, USER, FINAL)]
    IngestionService._fill_agents_workflow_root("p", events)
    assert other["input"] is None and recorded["input"] == "hi"
    assert "tracely.io_source" not in other["metadata"]
