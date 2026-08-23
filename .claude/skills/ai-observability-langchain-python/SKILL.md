---
name: ai-observability-langchain-python
description: PostHog AI Observability integration for LangChain (Python)
metadata:
  author: PostHog
  version: 1.48.0
---

# PostHog AI Observability for LangChain (Python)

Wire up PostHog's AI Observability so calls made through LangChain (Python) land in LLM Analytics as a full **session → trace → span → generation** tree — not just isolated `$ai_generation` events.

## Prerequisite — vendor LLM SDK

This skill instruments the LLM calls the project *already makes*. It does **not** install the vendor SDK for you.

Check the project's manifest for an LLM package. The catalog is far wider than the obvious providers — 68 variants covering agent frameworks (`openai-agents`, `claude-agent-sdk`, LangGraph, CrewAI, Mastra, …) and OpenAI-compatible gateways (Groq, OpenRouter, Together, Ollama, …), which an app reaches through the `openai` package plus a `baseURL` override. `1-begin.md` carries the ordered decision rules; follow them rather than matching on the first familiar package name. If no LLM SDK is present, switch to the `manual-capture` variant — it posts `$ai_generation` events directly and works standalone.

Everything else this skill needs — PostHog credentials, instrumentation packages, env vars — the skill installs and configures itself. It does **not** require a pre-existing `posthog.init(...)`. If one is already there, reuse its env-var names in `3-instrument.md`; if not, that step sets fresh values via `set_env_values`.

## Steps

Read every referenced file **before editing**. Then work through them in order:

1. **Begin** — see `references/1-begin.md`. Pick the variant with the ordered rules (framework before provider, gateway base URL before the SDK it borrows), then read four facts from the code: the conversation, the user, the turn, and whether the app registers tools.
2. **Install** — see `references/2-install.md`. Declare the variant's packages in the manifest — and only those. For providers and gateways that's the PostHog SDK alongside the vendor SDK, with no OpenTelemetry packages.
3. **Instrument** — see `references/3-instrument.md`. Swap the vendor client for PostHog's wrapper, attach `$ai_session_id`, a per-turn `posthog_trace_id`, and the distinct id to every call, and capture tool runs as `$ai_span` events. This step is what turns isolated generations into a session tree.
4. **Verify** — see `references/4-verify.md`. Describe a request the user can trigger, and grade what lands in PostHog — one session, grouped traces, right attribution — rather than what the diff contains.

## Reference files

- `references/1-begin.md` - Pick the variant that matches this project, then read the four facts the instrumentation needs
- `references/2-install.md` - Declare the packages the variant needs, and no others
- `references/3-instrument.md` - Swap in the wrapper client, then attach identity and tool spans so the calls form a session tree
- `references/4-verify.md` - Give the user a way to trigger one turn, and grade the tree that reaches PostHog
- `references/langchain.md` - Langchain ai observability installation - docs
- `references/python.md` - Python - docs
- `references/basics.md` - Ai observability basics - docs
- `references/generations.md` - Generations - docs
- `references/traces.md` - Traces - docs
- `references/sessions.md` - Sessions - docs
- `references/spans.md` - SPAns - docs
- `references/COMMANDMENTS.md` - Framework-specific rules the integration must follow

The linked install page carries the exact code blocks for this variant's language. Prefer copying from there over reconstructing from memory — package names and initialization shapes change between AIO releases.

## Key principles

- **Environment variables.** Read `<ph_project_token>` and `<ph_client_api_host>` from env, using the framework's env-var convention. Never hardcode either value.
- **The SDK wrapper is the default, not OpenTelemetry.** OTel makes the session tree awkward to build and maintain, so provider and gateway variants use PostHog's drop-in wrapper client. Reserve OTel for the `opentelemetry-*` variants and LlamaIndex, and never swap a framework's own tracing hook for an instrumentor.
- **Minimal changes.** The wrapper swaps a client constructor and adds parameters to existing calls. Don't restructure the app, and don't wrap the setup in an init function or module globals.
- **Match the docs.** Package names and wrapper imports change between AIO releases. The install page for this variant is the source of truth.
- **Cardinality is what gets graded.** One `$ai_session_id` per conversation, one `posthog_trace_id` per turn, shared by every call in it. An id minted per call is worse than none — it looks instrumented and groups nothing.
- **Tools become spans.** When the app registers tools, capture each execution as an `$ai_span` event sharing the turn's trace id — the wrapper never sees your dispatch loop. Framework variants emit these themselves; an app with no tools correctly has none.
- **Don't touch what isn't yours.** This skill instruments LLM observability only — generations, traces, sessions, spans. Identify calls, event tracking, error tracking, and dashboards belong to the base `integration` skill — do not add or edit them here.

## Emit a run record

When you finish, write `.posthog-wizard-cache/.posthog-ai.json` at the project root:

```json
{ "provider": "openai", "package": "@posthog/ai", "otel_init_file": "src/instrumentation.ts" }
```

`otel_init_file` keeps its name for the report's sake, but on the wrapper path there is no OTel init — set it to the file where the wrapper client was constructed (or, on the manual path, where the capture helper lives).

The `report/` step reads this file to render an AI Observability section in the setup report. If the cache directory does not exist, create it.

## Framework guidelines

- A missing PostHog configuration must never break the app — read keys optionally (never a required setting), guard init and capture behind their presence, and keep build and boot working with no PostHog environment set — but never silently: in development or debug builds fail loudly, using the language's idiomatic error, with the message "<VAR> variable required by PostHog is missing or un-configured, this causes events to be silently missed. This error stops appearing once <VAR> is configured" (substituting the actual variable name); production stays a no-op
- AI Observability carve-out: this skill instruments LLM calls and is not product-analytics coverage. Do NOT add posthog.capture() events for user actions, captureException() error handlers, or a reverse proxy unless the user explicitly asks for them
- AI Observability carve-out: only the wrapper-client and manual-capture install paths construct a PostHog client. The OTel and framework-hook paths have no client at all, so any rule in this file about the Posthog()/PostHog() constructor, exception autocapture, atexit/shutdown registration or flushing simply does not apply — never invent a client just to satisfy one
- AI Observability carve-out: the $ai_* payload properties ($ai_input, $ai_output_choices, and the rest) intentionally carry user-generated prompt and completion text, so this file's PII rules do NOT apply to them. Those rules still govern every other property you set
- AI Observability carve-out: read the PostHog key and host exactly as the variant's install doc reads them. A direct os.environ["POSTHOG_API_KEY"] / process.env lookup already fails loudly and idiomatically when unset, which satisfies this file's missing-configuration rule — do NOT add a separate presence check, guard branch, or custom raise around a bootstrap that is only a few lines long
- Remember that source code is available in the venv/site-packages directory
- posthog is the Python SDK package name
- Install dependencies with `pip install posthog` or `pip install -r requirements.txt` and do NOT use unquoted version specifiers like `>=` directly in shell commands
- In CLIs and scripts: MUST call posthog.shutdown() before exit or all events are lost
- Always use the Posthog() class constructor (instance-based API) instead of module-level posthog.api_key config
- Always include enable_exception_autocapture=True in the Posthog() constructor to automatically track exceptions
- NEVER send PII in capture() event properties — no emails, full names, phone numbers, physical addresses, IP addresses, or user-generated content
- PII belongs in identify() person properties, NOT in capture() event properties. Safe event properties are metadata like message_length, form_type, boolean flags.
- Register posthog_client.shutdown with atexit.register() to ensure all events are flushed on exit
- The Python SDK has NO identify() method — use posthog_client.set(distinct_id=user_id, properties={...}) to set person properties, or use identify_context(user_id) within a context
