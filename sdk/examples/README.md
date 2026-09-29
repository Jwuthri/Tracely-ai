# `sdk/examples/` — one runnable example per way of tracing

Every example is the **same realistic two-agent conversation**: a **Support Agent** answers
order/inventory questions over several turns by calling tools against a fake e-commerce DB
([`_fake_db.py`](_fake_db.py)) — `get_order_status` + `check_inventory` — then **hands off** the final
pricing-comparison turn to a **Billing Agent** (`compare_prices`). So each file shows a real
multi-turn, multi-agent tool-calling loop (plus thinking, in the `@observe`/manual ones), not a toy
single call. Each guards on its instrumentor + API key, printing setup hints instead of crashing when
a dependency or key is missing.

Each run also declares its two-agent catalog once via `tracely.trace(agents=AGENTS)` (so the
Conversation Agents panel + the judge's `@AGENTS` see it) and tags itself with its own filename
via `example=os.path.basename(__file__)`, so each span carries `tracely.metadata.example = <file>.py`
— filter on it in the Tracely UI to find the traces a given example produced.

```bash
pip install "tracely-ai[<extra>]"     # the extra named in each file's header
export TRACELY_API=http://localhost:8000           # read by these example scripts (init() itself takes endpoint=)
export OPENAI_API_KEY=sk-...                        # (or the provider's key)
uv run python sdk/examples/<file>.py
```

## Ways of sending traces (the SDK layers)

| File | Layer | Shows |
|---|---|---|
| [`auto_agent.py`](auto_agent.py) | L2 + L3 | `@observe` agent + `trace()` → **AGENT → thinking · 2 generations · 2 tools** tree |
| [`dropin_openai.py`](dropin_openai.py) | R13 | non-patching `wrap_openai` — the tool-calling agent, nothing patched globally |
| [`dropin_anthropic.py`](dropin_anthropic.py) | R13 | non-patching `wrap_anthropic` — the Claude tool-calling agent |
| [`manual_spans.py`](manual_spans.py) | L4 | the manual escape hatch — full agent: thinking → llm → tools (one **errors**) → answer (no key needed) |

## Frontier providers (L1 — auto-instrumented, zero span code)

| File | `instrument=` | Extra | Key |
|---|---|---|---|
| [`auto_openai.py`](auto_openai.py) | `["openai"]` | `[openai]` | `OPENAI_API_KEY` |
| [`auto_anthropic.py`](auto_anthropic.py) | `["anthropic"]` | `[anthropic]` | `ANTHROPIC_API_KEY` |
| [`auto_gemini.py`](auto_gemini.py) | `["gemini"]` | `[google]` | `GEMINI_API_KEY` |
| [`auto_mistral.py`](auto_mistral.py) | `["mistral"]` | `[mistral]` | `MISTRAL_API_KEY` |
| [`auto_bedrock.py`](auto_bedrock.py) | `["bedrock"]` | `[bedrock]` | AWS creds + `AWS_REGION` |
| [`auto_openrouter.py`](auto_openrouter.py) | `["langchain"]` | `[langchain,openrouter]` | `OPENROUTER_API_KEY` |

> **OpenRouter** routes one API to 100+ models. `auto_openrouter.py` uses LangChain's first-party
> `ChatOpenRouter` (`langchain-openrouter`) inside `create_agent`, traced by the LangChain
> instrumentor. (OpenRouter is also OpenAI-wire-compatible, so pointing the OpenAI SDK at its
> `base_url` works too — traced by the OpenAI instrumentor.)

## Harnesses (L1 — orchestration frameworks)

| File | `instrument=` | Extra | Agent pattern (current API) |
|---|---|---|---|
| [`auto_langchain.py`](auto_langchain.py) | `["langchain"]` | `[langchain]` | `langchain.agents.create_agent` (LangChain 1.0+; replaces `AgentExecutor`/`create_react_agent`) |
| [`auto_langgraph.py`](auto_langgraph.py) | `["langchain"]` | `[langchain]` | a custom `StateGraph` + `ToolNode` + `tools_condition` (hand-built ReAct loop) |
| [`auto_litellm.py`](auto_litellm.py) | `["litellm"]` | `[litellm]` | OpenAI-shaped tool-calling loop via one callback |
| [`auto_llama_index.py`](auto_llama_index.py) | `["llama-index"]` | `[llama-index]` | `ReActAgent` over `FunctionTool`s |
| [`auto_crewai.py`](auto_crewai.py) | `["crewai"]` | `[crewai]` | a `Crew` whose agent is equipped with the tools |

> Of these, `instrument="auto"` only auto-activates **langchain** — `litellm`, `llama-index` and
> `crewai` are deliberately opt-in via the explicit list shown per file. When the LangChain
> instrumentor is present it owns the LLM spans and the provider instrumentors are skipped to avoid
> duplicate spans (override with an explicit list). See the docs [Automatic instrumentation](../../docs/pages/automatic.mdx) page.

## Agent frameworks — first-party SDKs (L1)

The big labs now ship their own agent harnesses; each has an OpenInference instrumentor that
`init(instrument=[...])` activates, emitting AGENT/TOOL/LLM spans to Tracely.

| File | `instrument=` | Extra (+ SDK) | Framework |
|---|---|---|---|
| [`auto_openai_agents.py`](auto_openai_agents.py) | `["openai-agents"]` | `[openai-agents]` + `openai-agents` | OpenAI Agents SDK (`agents`: `Agent`/`Runner`/`@function_tool`) |
| [`auto_claude_agent.py`](auto_claude_agent.py) | `["claude-agent-sdk"]` | `[claude-agent-sdk]` + `claude-agent-sdk` | Anthropic Claude Agent SDK (`@tool`/`create_sdk_mcp_server`/`ClaudeSDKClient`; needs the Claude Code CLI) |
| [`auto_google_adk.py`](auto_google_adk.py) | `["google-adk"]` | `[google-adk]` + `google-adk` | Google ADK (`google.adk.agents.Agent` + `InMemoryRunner`; instrument **before** importing `google.adk`) |

## Demo data & CI-gate examples (not tracing how-tos)

| File | Shows |
|---|---|
| [`seed_conversations.py`](seed_conversations.py) | rich manual-API demo data — every observation type, plus the multi-agent shapes: a 5-specialist fan-out with skills + sub-agents, a coding swarm, a routing miss (`make seed-demo`) |
| [`seed_multiturn.py`](seed_multiturn.py) | one multi-turn conversation via the manual API (no key) — the showcase for the **rolling summary** + **declared agents** (`tracely.trace(agents=...)`) |
| [`seed_regression.py`](seed_regression.py) | promote a failing trace → red→green CI gates (`make seed-regression`) |
| [`seed_multicall.py`](seed_multicall.py) / [`seed_handler.py`](seed_handler.py) | repeated-call + handler fixtures for hermetic replay |
| [`weather_agent.py`](weather_agent.py) / [`weather_agent_cli.py`](weather_agent_cli.py) | a real agent wired for `tracely replay --entrypoint` / `--cmd` |
