# Automatic instrumentation — the out-of-the-box path

Add `tracely.init()` and existing provider/framework code is traced with **no span code**: model,
messages, token usage (streaming included), latency, tool calls and errors.

## Install with the right extra

```bash
pip install "tracely-ai[openai]"
```

| Group | Extras |
|---|---|
| Providers | `openai` · `anthropic` · `google` · `mistral` · `bedrock` · `groq` |
| Harnesses | `langchain` (covers LangGraph) · `llama-index` · `crewai` · `litellm` · `openrouter` |
| Agent SDKs | `openai-agents` · `google-adk` · `claude-agent-sdk` |
| Drop-ins (no instrumentor) | `xai` · `atlascloud` · `openrouter-openai` · `google-genai` · `mistral-sdk` |
| Everything | `all` (the four drop-ins and `openllmetry` stay separate installs) |

Extras install [OpenInference](https://github.com/Arize-ai/openinference) packages by default.
[OpenLLMetry](https://github.com/traceloop/openllmetry) is an equivalent alternative that
`init("auto")` also detects (`pip install "tracely-ai[openllmetry]"`); the backend ingests both
conventions independently.

## `init(...)`

```python
tracely.init(
    endpoint="http://localhost:8000",
    api_key=os.environ["TRACELY_KEY"],
    service_name="weather-agent",
    env="prod",                 # prod | staging | ci | dev
    instrument="auto",
    redact=False,
)
```

`instrument` takes:

- `"auto"` — probe for the five SDKs whose presence implies intent (**openai, anthropic, google,
  mistral, langchain**) and activate what's installed.
- an explicit list — `["openai", "langchain"]` — honoured as-is. Everything else (`crewai`,
  `llama-index`, `litellm`, `bedrock`, `groq`, the agent SDKs) is **opt-in this way**: a router or
  boto3 merely being importable doesn't mean the user wants it traced.
- a single string, or `False` for export-only (manual API).

Idempotent. The exporter is built on the **first** call and reused, so `redact` must be set there.

## `trace(...)` — run context

Instrumentor spans know nothing about Tracely. `trace()` stamps the run's identity onto every span
inside it via a custom `SpanProcessor`:

```python
with tracely.trace(agent="weather-agent", tenant="acme", conversation="conv-1", turn=3,
                   user="u_7", env="prod", plan="pro"):
    ...
```

- Opens **no span** — it's a context marker. Nested `trace()`s merge over the enclosing one.
- Works as a decorator, sync or async.
- `tenant=` — one codebase, many customers/workspaces/bots: each tenant is registered as its own
  Agent (endpoint, scenarios, gate, clusters, cases are per-tenant; the traces list filters by it),
  while `agent=` stays the per-span label in the Agent column. Unset → `agent` is the Agent.
- Extra kwargs become metadata (`plan="pro"` above).
- `traceparent=` joins a trace Tracely already minted (see § serving an agent Tracely drives).
- `agents=[...]` declares the agent catalog (see `manual.md`).

## `@observe` — function-level spans

```python
@tracely.observe(as_type="tool")
def get_weather(city: str) -> dict: ...

@tracely.observe(as_type="agent")
async def plan(goal: str) -> str: ...
```

Args → input, return → output, latency and exceptions (→ `level=ERROR`) captured; auto-nested by
OTel context with no manual parent wiring. `capture_input` / `capture_output` default `True` — turn
them off for large or sensitive payloads. `as_type` ∈ `span` · `generation` · `agent` · `delegate` ·
`tool` · `skill` · `chain` · `retriever` · `thinking` · `embedding` · `guardrail`.

Bonus: `@observe(as_type="tool")` functions are **transparently hermetic** under replay — they serve
recorded outputs instead of running, and re-raise recorded failures as `tracely.ToolError`.

## The layers compose

```
L1  auto-instrumentors    OpenAI · Anthropic · LangChain · LiteLLM   ← default, zero span code
L2  @observe(as_type=…)   your functions / agents / tools            ← one decorator
L3  tracely.trace(…)      run context (agent/conversation/turn/user) ← flows onto every span
L4  with tracely.llm(…)   manual spans                               ← escape hatch
```

All four nest into one trace. Use as much or as little as needed.

## LangChain & LangGraph

```bash
pip install "tracely-ai[langchain]"
```

Chains, agents and **LangGraph** graphs trace end-to-end; `init()` auto-registers the callback
handler. A graph is a `CHAIN` span, each node a child `CHAIN` (node name + step number become
`step_name` / `step_id`), LLM calls inside are `GENERATION` spans. Node return values are captured
as **state deltas** automatically — the Conversation State drawer works with no extra code.

Use the current LangChain 1.0+ API:

```python
from langchain.agents import create_agent
agent = create_agent("openai:gpt-5.4-mini", tools=[get_order_status], system_prompt="…")
```

**De-dup rule.** LangChain calls providers through their SDKs, so running both instrumentors
double-traces. Under `"auto"`, when the LangChain instrumentor is installed it owns the LLM spans
and the provider instrumentors are skipped. Want both (direct OpenAI calls *plus* chains)? Pass
`instrument=["openai", "langchain"]` deliberately. The same rule applies to CrewAI and LlamaIndex.

## Agent SDKs

| Framework | `instrument=` | Extra |
|---|---|---|
| OpenAI Agents SDK (`agents`) | `["openai-agents"]` | `[openai-agents]` |
| Anthropic Claude Agent SDK | `["claude-agent-sdk"]` | `[claude-agent-sdk]` |
| Google ADK (`google.adk`) | `["google-adk"]` | `[google-adk]` |

**Google ADK patches at import time** — `init(instrument=["google-adk"])` must run *before*
`import google.adk`. The Claude Agent SDK needs the Claude Code CLI installed and is async-only.

## LiteLLM

```python
tracely.init(instrument=["litellm"])   # wires litellm.callbacks = ["otel"]
```

Opt-in, and excluded from `"auto"`, because a call traced by both a provider instrumentor and
LiteLLM's OTel callback appears twice. Running both deliberately? Disable the overlap with
`OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`.

## Non-patching drop-ins

Wrap a client instance instead of monkey-patching globally:

```python
from tracely_sdk.openai import OpenAI, wrap_openai
client = OpenAI()                    # pre-wrapped
client = wrap_openai(existing)       # or wrap one you built
```

Seven presets: `openai`, `anthropic`, `google`, `mistral`, plus OpenAI-compatible `openrouter`,
`xai` (`from tracely_sdk.xai import Grok`) and `atlascloud` (`from tracely_sdk.atlascloud import AtlasCloud`). Same attributes as the manual `llm()` helper, so they
inherit `trace()` context. Non-streaming sync + async capture model · messages · output · usage ·
tool calls; for **full streaming capture prefer the instrumentor path**.

## OpenRouter and OpenAI-compatible gateways

Either works, no special instrumentor:

```python
from langchain_openrouter import ChatOpenRouter          # traced by the langchain instrumentor
agent = create_agent(ChatOpenRouter(model="anthropic/claude-3.5-sonnet"), tools=[...])

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
```

The routed `vendor/model` id flows into `model_id` and cost is derived from it (the rate table
matches on substring).

## Redaction

Happens **at export** — the one point every span passes through — so it covers auto-captured prompts
and completions, not just your own `set_io` payloads.

```python
tracely.init(redact=True)                        # built-in PII patterns: email, phone, SSN, card-shaped digits
tracely.init(redact=[r"ORD-\d+", r"acct_\w+"])   # your regexes → [REDACTED]
tracely.init(redact=lambda key, value: scrub(value))
```

Off by default; must be set on the first `init()`.

## Threads

Auto-nesting is contextvar-based (in-process). To trace work on another thread:

```python
th = tracely.run_in_thread(do_work, arg)   # inherits current span + trace() context
th.join(); result = th.result
```

## Serving an agent that Tracely drives

When Tracely calls your endpoint (`tracely simulate`, a scenario gate, the Emulate tab) it mints the
trace id, sends it as a W3C `traceparent` header, and POSTs its conversation id under the endpoint's
`session_key`. Honour both or the graded turn is Tracely's own span alone:

```python
@app.post("/agent/chat")
def chat(body: dict, request: Request):
    with tracely.trace(agent="support-agent",
                       conversation=body.get("conversation_id"),
                       traceparent=request.headers.get("traceparent")):
        return {"reply": run_agent(body["messages"])}
```

A malformed header is logged and ignored, never fatal. Already running OTel HTTP server
instrumentation (FastAPI/Starlette/Flask/Express)? The incoming context is ambient — pass
`conversation` and leave `traceparent` unset.

## Other languages / existing OpenTelemetry

Tracely reads **conventions, not libraries**. Point any OTLP/HTTP exporter at
`POST {endpoint}/v1/traces` with `Authorization: Bearer <ingest-key>`:

| What you emit | Recognised as |
|---|---|
| OpenInference (`openinference.span.kind`, `llm.*`) | span type, model, tokens, messages, tool calls |
| OTel GenAI semconv (`gen_ai.*`), attribute or event form | span type, model, tokens, messages, agent name, conversation id |
| OpenLLMetry / Traceloop (`gen_ai.prompt.<i>.*`, `traceloop.*`) | span type, model, tokens, messages |
| Vercel AI SDK `experimental_telemetry` (`ai.*`) | generations, tool calls with args + results, model, tokens |
| LiteLLM callback blobs (`llm.openai.*`) | model, tokens, messages |

That table is the TypeScript story today:

```ts
const result = await generateText({
  model: openai("gpt-4o"),
  prompt: "Summarize this ticket",
  experimental_telemetry: { isEnabled: true },
});
```

**Set two attributes by hand whatever the stack** — they are what turns a pile of spans into a
conversation:

- `tracely.conversation.id` (or semconv `gen_ai.conversation.id`) — threads turns together.
- `tracely.agent.id` — names the agent that gates and clusters group by. This one exactly: a
  framework's `gen_ai.agent.name` is NOT read (it names every sub-agent a harness spins up). The
  Python SDK sets it from `init(service_name=…)`; raw OTLP has to send it, or every trace lands
  under the workspace's single `default` agent.

Everything else is inferred. Failures need no special handling: an ERROR span status, a recorded
`exception` event, or an `error.type` attribute all mark the span failed.

The full indexed-attribute list is in the API reference: <https://doc.tracely-ai.com/api-reference>.

## Runnable examples

`sdk/examples/` has one file per way of tracing, all the same two-agent conversation:
`auto_openai.py` · `auto_anthropic.py` · `auto_gemini.py` · `auto_langchain.py` · `auto_langgraph.py`
· `auto_litellm.py` · `auto_openai_agents.py` · `auto_claude_agent.py` · `auto_google_adk.py` ·
`auto_agent.py` (`@observe` + `trace`) · `dropin_openai.py` · `manual_spans.py`.
