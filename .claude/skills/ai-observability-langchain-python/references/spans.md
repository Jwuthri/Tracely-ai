> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# Spans - Docs

Copy page

# Spans - Docs

Spans are units of work within an LLM [trace](/docs/ai-observability/traces.md). These are events that represent individual operations and discrete durations within your LLM application, like function calls, vector searches, or data retrieval steps, providing granular visibility into the execution flow.

![LLM trace tree](https://res.cloudinary.com/dmukukwp6/image/upload/llm_spans_151fd2701a.png)![LLM trace tree](https://res.cloudinary.com/dmukukwp6/image/upload/llm_spans_dark_6ce1cab5b9.png)

Spans are nested and displayed within a trace

PostHog captures spans to track atomic operations that make up your LLM workflow. For example:

-   **[Generations](/docs/ai-observability/generations.md)** - LLM calls and interactions
-   **Vector database searches** - Document and embedding retrieval
-   **Tool/function calls** - API calls, calculations, database queries
-   **RAG pipeline steps** - Retrieval, reranking, context building
-   **Data processing** - Validation, chunking, formatting

For technical implementation details, see [manual capture](/docs/ai-observability/installation/manual-capture.md).

## Event properties

A span is a single action within your application, such as a function call or vector database search.

**Event name**: `$ai_span`

### Core properties

| Property | Description |
| --- | --- |
| $ai_trace_id | The trace ID (a UUID to group related AI events together)Must contain only letters, numbers, and the following characters: -, _, ~, ., @, (, ), !, ', :, \|Example: d9222e05-8708-41b8-98ea-d4a21849e761 |
| $ai_session_id | (Optional) Groups related traces together. Use this to organize traces by whatever grouping makes sense for your application (user sessions, workflows, conversations, or other logical boundaries).Example: session-abc-123, conv-user-456 |
| $ai_span_id | (Optional) Unique identifier for this spanExample: bdf42359-9364-4db7-8958-c001f28c9255 |
| $ai_span_name | (Optional) The name of the spanExample: vector_search, data_retrieval, tool_call |
| $ai_parent_id | (Optional) Parent ID for tree view grouping (trace_id or another span_id)Example: 537b7988-0186-494f-a313-77a5a8f7db26 |
| $ai_latency | (Optional) The latency of the span in secondsExample: 0.361 |
| $ai_is_error | (Optional) Boolean to indicate if the span encountered an error |

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better