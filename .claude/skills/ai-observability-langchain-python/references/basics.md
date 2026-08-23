> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# AI Observability basics - Docs

Copy page

# AI Observability basics - Docs

This page covers how your LLM calls become analytics in PostHog and defines key concepts for AI Observability.

## How LLM calls become events

PostHog's AI Observability works by wrapping your existing LLM provider's SDK to capture requests and responses. Your API calls still go directly to your provider, but the wrapper extracts metadata from each call and sends it to PostHog as an event.

graph LR Call\["LLM call"\] --> Wrapper\["Wrapped SDK"\] Wrapper --> Provider\["LLM provider API"\] Wrapper --> Event\["Event"\] Event --> PostHog\["PostHog"\]

Events sent to PostHog are called **generations**. A generation represents a single LLM call. For example, when you send a prompt to Claude and get a response back, that's one generation.

Generations are represented using the event name `$ai_generation`. Each generation captures the model, provider, input, output, token counts, latency, and cost.

## Generation properties

### Tokens and costs

**Tokens** are the units LLMs use to process text. LLM providers charge based on token usage:

-   **Input tokens** are tokens used in the message you send to an LLM
-   **Output tokens** are tokens used in the message you receive from an LLM

PostHog automatically calculates costs by matching your model and provider against pricing data. We use OpenRouter's pricing as our primary source, with fallback to manually maintained pricing for additional models.

You can also [set custom pricing](/docs/ai-observability/calculating-costs.md) if you have negotiated rates or use unsupported models.

### Message roles

When you send messages to an LLM, each message has a **role** that tells the model how to interpret it:

| Role | Purpose | Example |
| --- | --- | --- |
| system | Instructions that define the assistant's behavior | "You are a helpful assistant that speaks like a pirate" |
| user | Messages from the end user | "What's the weather today?" |
| assistant | Previous model responses, used for conversation history | "Arrr, it be sunny with a chance of scurvy!" |

PostHog captures the full message array with roles intact, so you can see exactly what context the model had when it generated a response.

## Grouping related events

Most LLM applications involve multiple calls. Traces, spans, and sessions let you see how they connect:

graph TD Session\["Session"\] Trace\["Trace"\] Generation1\["Generation"\] Span\["Span"\] Generation2\["Generation"\] Session -.-> Trace Trace --> Generation1 Trace --> Span Span --> Generation2

Here's a breakdown of this hierarchy:

| Term | Definition | Example |
| --- | --- | --- |
| [Session](/docs/ai-observability/sessions.md) | Groups multiple traces together | A user's conversation thread |
| [Trace](/docs/ai-observability/traces.md) | Contains generations and spans for a single request | One chatbot message and response |
| [Span](/docs/ai-observability/spans.md) | Tracks an operation within a trace | A retrieval step or function call |
| [Generation](/docs/ai-observability/generations.md) | An LLM call, tracked as $ai_generation events | Sending a prompt to Claude |
| [Embedding](/docs/ai-observability/embeddings.md) | Converts text into vectors | Vectorizing documents for RAG |

**A session here usually means a conversation**

In AI Observability, a session (`$ai_session_id`) typically represents a single **conversation** – one thread of back-and-forth turns with your assistant. This is different from a standard PostHog [session](/docs/data/sessions.md) (`$session_id`), like the ones you see in [Session replay](/docs/session-replay.md), which represents one continuous visit to your app.

A single PostHog session can contain multiple AI Observability sessions: a user might start several separate conversations during one visit. See [sessions](/docs/ai-observability/sessions.md#note-on-posthog-session-ids) for more on the distinction.

## See it in action

Want to see these concepts applied in real projects? Check out our [demos and example projects](/docs/ai-observability/demos.md) or [browse AI Observability side projects](/side-projects?tag=ai-observability.md) built by the PostHog team.

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better