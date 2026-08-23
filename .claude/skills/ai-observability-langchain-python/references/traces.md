> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# Traces - Docs

Copy page

# Traces - Docs

Traces are a collection of [generations](/docs/ai-observability/generations.md) and [spans](/docs/ai-observability/spans.md) that capture a full interaction between a user and an LLM. The [traces tab](https://app.posthog.com/ai-observability/traces) lists them along with the properties autocaptured by PostHog like the person, total cost, total latency, and more.

## Sessions vs Traces

-   **Trace** (`$ai_trace_id`): Groups related generations and spans together. Required for all AI Observability events.
-   **Session** (`$ai_session_id`): Optional property that groups multiple traces together based on your chosen grouping strategy.

See the [Sessions](/docs/ai-observability/sessions.md) documentation for more details on how to use `$ai_session_id`.

## Trace timeline

Clicking on a trace opens a timeline of the interaction with all the generation and span events. The trace timeline enables you to see the entire conversation, profiling details, and the individual generations and spans.

![LLM traces](https://res.cloudinary.com/dmukukwp6/image/upload/llma_traces_25e203aa50.png)![LLM traces](https://res.cloudinary.com/dmukukwp6/image/upload/llma_traces_dark_dd6ad555dc.png)

A trace presents LLM event data in a timeline, tree-structured view

### Waterfall timeline

Above the trace tree, a waterfall timeline shows when each operation started and how long it ran. Each bar represents a generation, span, or embedding event, color-coded to match the tree view.

The waterfall helps you:

-   **See latency breakdown** — Identify which operations took the longest and where time was spent waiting.
-   **Spot concurrency** — Operations running in parallel appear on separate lanes. Parent-child connectors distinguish nested operations from concurrent siblings.
-   **Find errors at a glance** — Errored operations display a red border on their bar.
-   **Navigate the trace** — Click any bar to select the corresponding event in the tree view.

The time axis uses clock-friendly intervals (e.g. 15s, 30s, 1m, 2m). For deeply nested traces, the timeline caps at 6 visible lanes and scrolls vertically. You can collapse the timeline or expand it to full screen.

The waterfall also appears in the session view's steps drawer when inspecting a specific trace.

## Conversation display options

When viewing a trace, you can control how conversation messages are displayed using the display options dropdown. The available options are:

-   **Expand all** - Shows the full content of all messages in the conversation
-   **Expand user only** - Expands only user messages, keeping system and assistant messages collapsed for easier scanning of user inputs
-   **Collapse except output and last input** - The default view that shows the model's output and the most recent user input, keeping earlier messages collapsed

## Tool calls

Traces display any [tools](/docs/ai-observability/tools.md) called by the generations within them, shown as tags in the traces list. This makes it easy to see which conversations involved tool use at a glance.

## Sentiment classification

PostHog can classify the sentiment of user messages in a trace as negative, neutral, or positive using a local model — no data is sent to third-party services. Sentiment runs as a [sentiment analysis evaluation](/docs/ai-evals.md#sentiment-analysis-evaluations) that you configure. Each trace gets an overall sentiment label and score, with a per-generation and per-message breakdown. See [Sentiment classification](/docs/ai-observability/sentiment.md) for more details.

## Search traces with PostHog AI

[PostHog AI](/docs/posthog-ai.md) can search and analyze your LLM traces using natural language. When you're on an [AI Observability page](https://app.posthog.com/ai-observability), PostHog AI automatically switches to its AI Observability mode, giving it access to tools for searching traces by date range, model, cost, error status, and other properties.

Example prompts you can try:

-   "Show me recent LLM traces from the past week"
-   "What are the most expensive LLM calls from today?"
-   "Find traces with errors in the last 30 days"
-   "What's happening in my most expensive trace?"

PostHog AI returns trace details including name, latency, cost, token counts, and error count. It can also read individual traces to provide a detailed summary of what happened.

## AI event hierarchy

flowchart TD S\["<strong>$ai\_session\_id</strong><br/>(optional)"\] A\[<strong>$ai\_trace</strong>\] A2\[<strong>$ai\_trace</strong>\] B\[<strong>$ai\_generation</strong>\] C@{ shape: processes, label: "<strong>$ai\_spans</strong>" } D\[<strong>$ai\_generation</strong>\] E@{ shape: processes, label: "<strong>$ai\_spans</strong>" } F\[<strong>$ai\_generation</strong>\] S -.-> A S -.-> A2 A --> B A --> C C --> D C --> E E --> F

Traces consist of the following event hierarchy:

1.  (Optional) A session (`$ai_session_id`) can group multiple traces together.
2.  A trace (`$ai_trace_id`) is the top-level required grouping for LLM events.
3.  A trace can contain multiple spans and generations.
4.  A span can be the parent of other spans.
5.  A generation can be the child of a span or trace.

## Event properties

A trace is a group that contains multiple spans, generations, and embeddings. Traces can be manually sent as events or appear as pseudo-events automatically created from child events.

**Event name**: `$ai_trace`

### Core properties

| Property | Description |
| --- | --- |
| $ai_trace_id | The trace ID (a UUID to group related AI events together)Must contain only letters, numbers, and special characters: -, _, ~, ., @, (, ), !, ', :, \|Example: d9222e05-8708-41b8-98ea-d4a21849e761 |
| $ai_session_id | (Optional) Groups related traces together. Use this to organize traces by whatever grouping makes sense for your application (user sessions, workflows, conversations, or other logical boundaries).Must contain only letters, numbers, and special characters: -, _, ~, ., @, (, ), !, ', :, \|Example: session-abc-123, conv-user-456 |
| $ai_latency | (Optional) The latency of the trace in seconds |
| $ai_span_name | (Optional) The name of the traceExample: chat_completion, rag_pipeline |
| $ai_is_error | (Optional) Boolean to indicate if the trace encountered an error |
| $ai_error | (Optional) The error message or object if the trace failed |

### Pseudo-trace Events

When you send generation (`$ai_generation`), span (`$ai_span`), or embedding (`$ai_embedding`) events with a `$ai_trace_id`, PostHog automatically creates a pseudo-trace event that appears in the dashboard as a parent grouping. These pseudo-traces:

-   Are not actual events in your data
-   Automatically aggregate metrics from child events (latency, tokens, costs)
-   Provide a hierarchical view of your AI operations
-   Do not require sending an explicit `$ai_trace` event

This means you can either:

1.  Send explicit `$ai_trace` events to control the trace metadata
2.  Let PostHog automatically create pseudo-traces from your generation/span events

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better