> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# Sessions - Docs

Copy page

# Sessions - Docs

Sessions group multiple [traces](/docs/ai-observability/traces.md) together using the `$ai_session_id` property.

What constitutes a "session" is flexible and depends on your use case. The `$ai_session_id` property can represent whatever grouping makes sense for your application - whether that's user sessions, workflows, conversations, threads, or other logical boundaries.

## Benefits of using sessions

Grouping traces with the same session ID enables you to:

-   Track related interactions across multiple traces
-   Analyze aggregate metrics like total cost, tokens, or latency per session
-   Understand patterns across grouped interactions
-   Filter and segment your analytics by session-level attributes

## When to use sessions

Sessions are useful when you want to:

-   **Group related traces**: Connect multiple traces that are part of the same logical unit (visit, workflow, thread, etc.)
-   **Calculate aggregate metrics**: Total cost, tokens, or latency across a group of related traces
-   **Analyze patterns**: Understand how multiple traces relate to each other within a session
-   **Filter and segment**: Query and analyze your data by session-level groupings

## Traces vs Sessions

-   **Trace** (`$ai_trace_id`): Groups related AI events (generations, spans, embeddings) together. Required for all AI Observability events.
-   **Session** (`$ai_session_id`): Optional property that groups multiple traces together based on your chosen grouping strategy.

## How to set a session ID

Set the `$ai_session_id` property when capturing LLM events using the `posthogProperties` (JavaScript) or `posthog_properties` (Python) parameter in PostHog's AI Observability SDKs. Use any identifier that makes sense for grouping related traces in your application.

The `$ai_session_id` value must contain only letters, numbers, and the special characters `-`, `_`, `~`, `.`, `@`, `(`, `)`, `!`, `'`, `:`, and `|`.

## Analyzing sessions

Once you've set session IDs on your events, you can analyze them in several ways:

### In the Activity tab

View all traces and events for a specific session by filtering for the `$ai_session_id` property in the [Activity tab](https://app.posthog.com/events).

### In insights

Create insights that aggregate metrics by session:

-   Number of traces per session
-   Average cost per session
-   Total tokens used per session
-   Session duration
-   Patterns across sessions

### In the trace viewer

When viewing a [trace](/docs/ai-observability/traces.md), you can see which session it belongs to, helping you understand the broader context of related traces.

## Note on PostHog session IDs

`$ai_session_id` is different from PostHog's standard [`$session_id`](/docs/data/sessions.md). LLM events (traces, generations, spans) are often captured from your backend, where you don't have automatic access to the frontend session ID that PostHog normally tracks.

If you have your own custom session tracking and a way to pass your session ID from frontend to backend, you can use the same session ID for both. However, most applications will use different session identifiers:

-   **`$session_id`**: PostHog's auto-captured frontend session tracking (which you can overwrite if you want)
-   **`$ai_session_id`**: Your custom session identifier for grouping LLM interactions (typically managed by your backend)

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better