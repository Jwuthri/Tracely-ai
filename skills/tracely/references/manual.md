# Manual instrumentation — the full span API

Use this when the automatic path can't express what you need: custom retrievers, guardrails,
reasoning steps, multi-agent handoffs, multimodal I/O, a hand-rolled provider client, or a run whose
shape you want to control exactly. Manual spans **nest inside auto-instrumented traces** — mixing is
normal, not a fallback.

```python
tracely.init(endpoint=..., api_key=..., service_name="support-agent", env="prod")
# instrument="auto" optional here; pass False for a purely manual trace
```

## Span types — one context manager each

| Call | Type | Represents |
|---|---|---|
| `agent(slug, *, version, run_id, role, conversation, turn, user, trace_name, handoff_from, edge="delegate")` | `AGENT` | The run root — one turn. |
| `delegate(to, *, agent, task, edge="delegate")` | `DELEGATE` | A handover to another agent — the routing act itself. |
| `llm(model, *, agent, temperature, top_p, max_tokens, frequency_penalty, presence_penalty, seed, tool_calls, metadata)` | `GENERATION` | A model call. |
| `tool(name, *, agent)` | `TOOL` | A tool / function execution. |
| `skill(name, *, agent, version)` | `SKILL` | A named capability / playbook the agent chose to run. |
| `thinking(name="thinking", *, agent, model)` | `THINKING` | Reasoning, as its own span with its own token usage. |
| `retriever(name="retrieve", *, agent)` | `RETRIEVER` | A retrieval step (vector / keyword / web). |
| `embedding(model, *, agent)` | `EMBEDDING` | An embedding call. |
| `guardrail(name="guardrail", *, agent)` | `GUARDRAIL` | A safety / policy check. |
| `chain(name, *, agent)` | `CHAIN` | A grouping span (a named sub-pipeline). |
| `step(name, *, step_id)` / `turn(turn_id, *, index)` | `SPAN` / marker | Anything else. |

Nesting builds the tree; there is no manual parent wiring.

## Annotating a span

| Call | Writes |
|---|---|
| `set_io(span, *, input=None, output=None)` | `tracely.input` / `tracely.output` |
| `set_usage(span, *, input_tokens, output_tokens, thinking_tokens, cached_tokens, cache_write_tokens)` | `gen_ai.usage.*` |
| `set_metadata(span, **kv)` | `tracely.metadata.<key>` — searchable tags |
| `set_state(delta, span=None, *, max_bytes=4096)` | `tracely.state.<channel>` |
| `set_agents(span, agents)` | the agent catalog |
| `error(span, message="")` | `StatusCode.ERROR` → `level=ERROR` — **the** failure signal |
| `flush()` | force-flush the OTLP exporter |

**Cost is derived** from model name + token counts. Never trace a cost field.

## The run root

```python
with tracely.agent("support-agent", version="v4", conversation="conv-1", turn=0,
                   user="u_7741", trace_name="docs Q&A") as a:
    tracely.set_io(a,
        input={"role": "user", "content": [{"type": "text", "text": question}]},
        output={"role": "assistant", "content": [{"type": "text", "text": answer}]})
```

`version` is auto-registered into the agent registry — it's what the regression gate pins to.
`conversation` + `turn` are what thread turns into one replayable conversation.

## Generations

```python
with tracely.llm("gpt-4o", agent="support-agent",
                 temperature=0.7, top_p=1.0, max_tokens=1024, seed=7,
                 tool_calls=["get_weather"],                       # tools the model REQUESTED
                 metadata={"prompt_version": "v3", "tenant": "acme"}) as g:
    tracely.set_io(g, input=messages,
                      output={"role": "assistant", "content": answer, "finish_reason": "stop"})
    tracely.set_usage(g, input_tokens=760, output_tokens=88, thinking_tokens=40)
```

Sampling params become `gen_ai.request.*` and render in the generation's Metadata.

**`tool_calls=` is the silent-failure detector.** Record what the model asked for even when the tool
never ran — a requested-but-never-executed tool is exactly what the `tool_consistency` structural
evaluator catches. Don't emit a `tool()` span for a call that didn't happen.

## Tools, and failing correctly

```python
with tracely.tool("get_charges", agent="billing-agent") as t:
    tracely.set_io(t, input={"order_id": "ORD-4471"})
    try:
        tracely.set_io(t, output=charges_api(order_id))
    except Exception as e:
        tracely.error(t, f"billing upstream timeout: {e}")
```

An error swallowed into a successful-looking span is invisible to detection, clustering and the
gate. If the agent handles the error gracefully, still mark the tool span — the *handling* is what
you want graded, not a pretend success.

## Reasoning

```python
with tracely.thinking(agent="support-agent", model="gpt-4o") as th:
    tracely.set_io(th, output={"role": "thinking", "content": "Plan: search docs, then answer."})
    tracely.set_usage(th, thinking_tokens=120)
```

## RAG: guardrail → embedding → retriever, grouped in a chain

```python
with tracely.guardrail("input_guardrail", agent="support-agent") as gr:
    tracely.set_io(gr, input=question, output={"action": "allow", "flags": []})

with tracely.chain("rag_pipeline", agent="support-agent"):
    with tracely.embedding("text-embedding-3-small", agent="support-agent") as e:
        tracely.set_io(e, input=question, output={"dims": 1536})
        tracely.set_usage(e, input_tokens=12)
    with tracely.retriever("search_docs", agent="support-agent") as r:
        tracely.set_io(r, input={"query": question, "top_k": 3}, output={"hits": hits})
        tracely.set_metadata(r, vector_store="pgvector")
```

A blocked guardrail records `{"action": "block", "flags": [...]}` and the agent returns a safe
refusal.

## Skills — a named capability

Between a tool and an agent: a procedure the agent *chose* to run — a refund flow, an escalation
playbook, a loaded agent-skill file — with its own tools and generations nested inside.

```python
with tracely.skill("refund-flow", agent="billing-agent", version="v2") as sk:
    tracely.set_io(sk, input={"order_id": oid}, output={"refunded": True})
    with tracely.tool("issue_refund", agent="billing-agent"):
        ...
```

Naming it turns "which skill did this?" into a filter, a failure cluster and a gate assertion rather
than a shape you infer from the tree. `version` (→ `tracely.metadata.skill_version`) is what tells
you which revision changed when the skill starts failing.

## Multi-agent handoffs

Open the specialist's `agent(...)` **inside** the orchestrator's span and record the edge:

```python
with tracely.agent("router", role="orchestrator", conversation="c1") as root:
    with tracely.agent("billing-agent", role="specialist",
                       conversation="c1", handoff_from="router"):
        ...
```

`handoff_from` records `router → billing-agent` (`edge` defaults to `"delegate"`), which powers the
multi-agent graph view.

To grade the **routing decision** on its own, wrap the callee in a `delegate(...)` span instead:

```python
with tracely.agent("router", role="orchestrator", conversation="c1"):
    with tracely.delegate("billing-agent", agent="router", task="issue refund") as d:
        tracely.set_io(d, input={"reason": "user asked for a refund"}, output=result)
        with tracely.agent("billing-agent", role="specialist", conversation="c1"):
            ...
```

Same edge, plus a span a step-level judge can read. "Was billing the right agent for this?" and "did
billing do it well?" are different failures with different fixes, and only the delegate span
separates them.

## Structured I/O — how content renders

`set_io` accepts strings, but structured content renders richly and is self-describing:

- a bare message array `[{"role", "content"}]` → a transcript
- a single message object → one bubble
- typed content blocks → text + image thumbnail + file chip

```python
user_msg = {"role": "user", "content": [
    {"type": "text", "text": "My order arrived cracked — photo + receipt attached."},
    {"type": "image_url", "image_url": {"url": "https://…/photo.jpg"}},
    {"type": "input_file", "filename": "receipt.pdf", "url": "https://…/receipt.pdf",
     "mime_type": "application/pdf"},
]}
tracely.set_io(agent_span, input=user_msg)
```

Output is best as the completion message object the chat API returned, or a dict for a structured
result — emitted as-is.

## The agent catalog — `trace(agents=…)` / `set_agents(...)`

Tracing shows which agents *fired*; the catalog declares which agents, tools, prompts and models
*exist*. It fills the Conversation Agents panel and is readable from judge prompts as `@AGENTS`.

```python
AGENTS = [
    {
        "name": "support",
        "description": "front-line agent; routes billing questions",
        "system_prompt": "You are the support agent for Acme…",   # free-form keys kept verbatim
        "model": "gpt-5.2",
        "tools": {
            "lookup_order": {"name": "lookup_order", "description": "order by id",
                             "parameters": {"type": "object",
                                            "properties": {"order_id": {"type": "string"}}}},
        },
    },
    {"name": "billing", "description": "refunds and charges", "tools": {...}},
]

with tracely.trace(agent="support", conversation="conv-1", agents=AGENTS):
    ...
# or pin to one span: tracely.set_agents(root_span, AGENTS)
```

Only `name` / `description` / `tools` are interpreted (`tools` is a dict keyed by tool name, or a
list); any other key is stored and rendered verbatim. Non-Python services push the same catalog with
`POST /api/sessions/{conversation_id}/config` (`{"agents": [...]}`). Without a declaration Tracely
derives an observed view from the spans; a declared catalog wins.

## Shared state — `set_state(...)`

Record the **delta** a step wrote, never a snapshot. The State drawer folds deltas back into the
full object and shows the per-step diff, so a debug reads *"`plan` was emptied at step 4, in
`replan`"*.

```python
with tracely.step("planner") as s:
    tracely.set_state({"plan": plan, "retries": 0})      # scope = the span it's attached to
```

Each channel becomes its own `tracely.state.<channel>` attribute (not one blob), so a query can read
a single channel; values over `max_bytes` are truncated **per key** so one fat channel can't crowd
out the small ones. A "store" that outlives a turn is just state attached to the trace's root span.

**LangGraph needs no code** — node return values are captured as deltas automatically.

Caveat: the implicit span is the OTel *current* span, which exists inside `step()`/`tool()`/`llm()`
but **not** inside a framework callback (`trace()` is a context marker, not an active span, and
LangGraph runs nodes with no span in context). A bare `set_state()` there warns rather than dropping
the write — pass `span=` explicitly, or rely on the automatic capture.

## Hermetic replay seam — `call_tool` / `call_llm`

The same code runs live in prod and deterministically offline in CI:

```python
def run(user_input: str) -> str:
    with tracely.agent("support-agent"):
        order  = tracely.call_tool("get_order", lambda: get_order(order_id), args={"order_id": order_id})
        answer = tracely.call_llm("gpt-4o", lambda: chat(messages), input=messages, usage=(812, 96))
        return answer
```

- **Production** (no fixtures active): invokes your `fn`, records output/error on the span, returns it.
- **CI replay**: `tracely replay` activates the case's recorded fixture bundle; the recorded outputs
  are served in order (or matched by `args`) and your `fn` is never called.
- A call that **errored** in production is re-raised as `tracely.ToolError`, so the agent's own
  `try/except` runs exactly as it would live.

Most agents don't need this rewrite: `@observe(as_type="tool")` tools replay transparently, and
inside a `fixtures()` block Tracely class-patches OpenAI `chat.completions` and Anthropic `messages`
so direct SDK calls are served the recorded completion. Other providers fall back to live — use
`call_llm` there. Activate manually with `with tracely.fixtures(bundle): ...`; `fixture(kind, name)`
peeks the next recorded output without consuming it.

## Don't forget

```python
tracely.flush()   # before the process exits
```

## Worked examples

`sdk/examples/seed_conversations.py` exercises **every** helper across eleven scenarios — single and
multi-turn, multi-agent + handoffs, RAG, multimodal, structured output, tool error + recovery,
guardrail block, hallucination, silent tool, deep research. `make seed-demo` runs it.
`sdk/examples/manual_spans.py` is the manual API as a full agent with no provider key needed.
