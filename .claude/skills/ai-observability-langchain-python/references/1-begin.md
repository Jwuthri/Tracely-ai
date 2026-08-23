---
title: AI Observability Setup - Begin
description: Pick the variant that matches this project, then read the four facts the instrumentation needs
---

Pick the variant, then read the code. Do not edit anything in this step.

## Pick the variant

This skill ships 68 variants. Call `load_skill_menu` with `category: "ai-observability"`. That list is the source of truth.

Apply these rules in order. The first match wins. Frameworks wrap providers, and gateways look like OpenAI, so the order matters.

### 1. A framework wins over the provider under it

| Package in the manifest | Variant |
|---|---|
| `openai-agents` | `openai-agents` |
| `claude-agent-sdk` | `claude-agent-sdk` |
| `langchain`, `@langchain/core` | `langchain-{python,node}` |
| `langgraph`, `@langchain/langgraph` | `langgraph-{python,node}` |
| `ai` (Vercel AI SDK) | `vercel-ai` |
| `llama-index`, `llamaindex` | `llamaindex` |
| `crewai` | `crewai` |
| `pyautogen`, `autogen-agentchat` | `autogen` |
| `dspy`, `dspy-ai` | `dspy` |
| `pydantic-ai` | `pydantic-ai` |
| `semantic-kernel` | `semantic-kernel` |
| `smolagents` | `smolagents` |
| `mirascope` | `mirascope` |
| `instructor` | `instructor-{python,node}` |
| `litellm` | `litellm` |
| `mastra`, `@mastra/core` | `mastra` |
| `convex` | `convex` |

Instrument the framework, not the provider below it. A provider instrumentor keeps the model calls and loses the agent, tool, and handoff structure.

### 2. An `openai` client with a base URL override means a gateway

Most OpenAI-compatible providers ship no SDK. Apps call them with the `openai` package aimed at another host. Check the client constructor and `OPENAI_BASE_URL`. Common hosts are `api.groq.com`, `openrouter.ai`, `api.together.xyz`, and `localhost:11434`.

Pick the variant that names the provider. The install shape matches plain OpenAI, but the provider name does not. Step 3 explains why that matters.

### 3. A plain provider SDK maps to that provider

`openai`, `anthropic`, `@anthropic-ai/sdk`, `google-genai`, `@google/genai`, `mistralai`, and `cohere` each have a variant. `boto3` with `bedrock-runtime` maps to `aws-bedrock`.

### 4. Anything else

- Several candidates and no framework: prefer the higher-level one. If it stays unclear, use `wizard_ask` with the candidates as options.
- The app already emits its own OTel spans: `opentelemetry-{python,node}`.
- No LLM SDK at all: `manual-capture`.

Language follows the manifest. A `package.json` means Node. A `pyproject.toml` or `requirements.txt` means Python. Framework variants have no language suffix.

Report the variant and the reason in a `[STATUS]` line, then call `install_skill` with the full id.

## Read four facts from the code

The install doc holds the code. It cannot know this app. Step 3 uses these answers and nothing else.

1. **Conversation.** The field that groups turns, such as `thread_id` or `conversation_id`. If the app has none, the process run is the conversation.
2. **User.** The user id in scope at the call sites. If the app has none, the events stay anonymous. Do not invent one.
3. **Turn.** The function that takes one question and returns one answer. It may call the model several times.
4. **Tools.** Does the app register tools with its calls? Look for a `tools=` argument or a tool decorator. Find the loop that runs them.

Note the module that builds the vendor client. Step 3 replaces that constructor. If the project already calls `posthog.init(...)` or `PostHog(...)`, reuse its env-var names and its client.

Go to `2-install.md`.

---

**Upon completion, continue with:** [2-install.md](2-install.md)