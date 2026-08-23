> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# Generations - Docs

Copy page

# Generations - Docs

Generations are events that capture LLM calls and their responses. They represent interactions and conversations with an AI model. Generations are tracked as `$ai_generation` events and can be used to create and visualize [insights](/docs/product-analytics/insights.md) just like other PostHog events.

![$ai_generation events](https://res.cloudinary.com/dmukukwp6/image/upload/ai_generations_f687da8aaa.png)![$ai_generation events](https://res.cloudinary.com/dmukukwp6/image/upload/ai_generations_dark_51d8bae273.png)

View recent AI generation events in the **Activity** tab

The **AI Observability** > [**Generations** tab](https://app.posthog.com/ai-observability/generations) displays a list of generations, along with a preview of key autocaptured properties. You can filter and search for generations by various properties.

![LLM generations](https://res.cloudinary.com/dmukukwp6/image/upload/llm_generations_b12119af33.png)![LLM generations](https://res.cloudinary.com/dmukukwp6/image/upload/llm_geneerations_dark_03c996e8ad.png)

Preview and filter AI generations in the **Generations** tab

## What does each generation capture?

A generation event records the AI model’s inputs, generated output, and additional metadata – like token usage, latency, and cost – for each LLM call.

PostHog automatically logs and displays the generation and its data within a conversation view for contextual debugging and analysis. You can also view the raw JSON payload.

You can expect each generation to have the following properties (in addition to the [default event properties](/docs/data/events.md#default-properties)):

| Property | Description |
| --- | --- |
| $ai_model | The specific model, like gpt-5-mini or claude-4-sonnet |
| $ai_latency | The latency of the LLM call in seconds |
| $ai_time_to_first_token | Time to first token in seconds (streaming only) |
| $ai_tools | Tools and functions available to the LLM |
| $ai_input | List of messages sent to the LLM |
| $ai_input_tokens | The number of tokens in the input (often found in response.usage) |
| $ai_output_choices | List of response choices from the LLM |
| $ai_output_tokens | The number of tokens in the output (often found in response.usage) |
| $ai_total_cost_usd | The total cost in USD (input + output) |
| [[...]](/docs/ai-observability/generations.md#event-properties) | See [full list](/docs/ai-observability/generations.md#event-properties) of properties |

When calling LLMs with our [SDK wrappers](/docs/ai-observability/installation.md), you can also enrich the `$ai_generation` event with your own [custom properties](/docs/ai-observability/custom-properties.md) and PostHog attributes like groups and distinct IDs for identified users.

PostHog AI

### Python

```python
response = client.responses.create(
    model="gpt-5-mini",
    input=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs"}
    ],
    posthog_distinct_id="user_123", # optional
    posthog_trace_id="trace_123", # optional
    posthog_properties={"custom_property": "value"}, # optional
    posthog_groups={"company": "company_id_in_your_db"},  # optional
    posthog_privacy_mode=False # optional
)
```

### TypeScript

```typescript
const completion = await openai.responses.create({
    model: "gpt-5-mini",
    input: [{ role: "user", content: "Tell me a fun fact about hedgehogs" }],
    posthogDistinctId: "user_123", // optional
    posthogTraceId: "trace_123", // optional
    posthogProperties: { custom_property: "value" }, // optional
    posthogGroups: { company: "company_id_in_your_db" }, // optional
    posthogPrivacyMode: false // optional
});
```

## How are generations, traces, and spans related?

Generations are nested under [spans](/docs/ai-observability/spans.md) and [traces](/docs/ai-observability/traces.md).

A trace is the top-level entity that groups all related LLM operations, including spans and generations, together.

Spans are individual operations within a trace. Some spans represent generations, which are also uniquely identified using the `$ai_span_id` property. However, most spans track other types of LLM operations such as tool calls, RAG retrieval, data processing, and more.

![LLM trace tree](https://res.cloudinary.com/dmukukwp6/image/upload/llm_spans_151fd2701a.png)![LLM trace tree](https://res.cloudinary.com/dmukukwp6/image/upload/llm_spans_dark_6ce1cab5b9.png)

Generations and spans are nested within a trace

## Tool calls

When a generation includes tool calls (function calls), PostHog automatically extracts them and displays them as tags on the generation. You can see aggregated tool usage across all your generations in the [Tools](/docs/ai-observability/tools.md) tab.

## Evaluating generations

You can automatically assess the quality of your generations using [evaluations](/docs/ai-evals.md). Evaluations use an LLM-as-a-judge approach to score outputs based on criteria like relevance, helpfulness, or safety.

## Sentiment classification

PostHog can classify the sentiment of user messages in your generations as negative, neutral, or positive using a local model — no data is sent to third-party services. Sentiment runs as a [sentiment analysis evaluation](/docs/ai-evals.md#sentiment-analysis-evaluations) that you configure. See [Sentiment classification](/docs/ai-observability/sentiment.md) for more details.

## Event properties

A generation is a single call to an LLM.

**Event name**: `$ai_generation`

### Core properties

| Property | Description |
| --- | --- |
| $ai_trace_id | The trace ID (a UUID to group AI events) like conversation_idMust contain only letters, numbers, and special characters: -, _, ~, ., @, (, ), !, ', :, \|Example: d9222e05-8708-41b8-98ea-d4a21849e761 |
| $ai_session_id | (Optional) Groups related traces together. Use this to organize traces by whatever grouping makes sense for your application (user sessions, workflows, conversations, or other logical boundaries).Example: session-abc-123, conv-user-456 |
| $ai_span_id | (Optional) Unique identifier for this generation |
| $ai_span_name | (Optional) Name given to this generationExample: summarize_text |
| $ai_parent_id | (Optional) Parent span ID for tree view grouping |
| $ai_model | The model usedExample: gpt-5-mini |
| $ai_provider | The LLM providerExample: openai, anthropic, gemini |
| $ai_input_tokens | The number of tokens in the input (often found in response.usage) |
| $ai_output_tokens | The number of tokens in the output (often found in response.usage) |
| $ai_latency | (Optional) The latency of the LLM call in seconds |
| $ai_time_to_first_token | (Optional) Time to first token in seconds. Only applicable for streaming responses. |
| $ai_http_status | (Optional) The HTTP status code of the response |
| $ai_base_url | (Optional) The base URL of the LLM providerExample: https://api.openai.com/v1 |
| $ai_request_url | (Optional) The full URL of the request made to the LLM APIExample: https://api.openai.com/v1/chat/completions |
| $ai_is_error | (Optional) Boolean to indicate if the request was an error |
| $ai_error | (Optional) The error message or object |
| $ai_stop_reason | (Optional) The reason the model stopped generating tokensExample: end_turn, stop, max_tokens, tool_use |

### Cost properties

Cost properties are optional as we can automatically calculate them from model and token counts. If you want, you can provide your own cost properties or custom pricing instead.

#### Pre-calculated costs

| Property | Description |
| --- | --- |
| $ai_input_cost_usd | (Optional) The cost in USD of the input tokens |
| $ai_output_cost_usd | (Optional) The cost in USD of the output tokens |
| $ai_request_cost_usd | (Optional) The cost in USD for the requests |
| $ai_web_search_cost_usd | (Optional) The cost in USD for the web searches |
| $ai_total_cost_usd | (Optional) The total cost in USD (sum of all cost components) |

#### Custom pricing

| Property | Description |
| --- | --- |
| $ai_input_token_price | (Optional) Price per input token (used to calculate $ai_input_cost_usd) |
| $ai_output_token_price | (Optional) Price per output token (used to calculate $ai_output_cost_usd) |
| $ai_cache_read_token_price | (Optional) Price per cached token read |
| $ai_cache_write_token_price | (Optional) Price per cached token write. For custom Anthropic pricing, this applies to both cache TTLs unless $ai_cache_write_1h_token_price is set. |
| $ai_cache_write_1h_token_price | (Optional) Price per token written to Anthropic's 1-hour cache. Takes precedence over $ai_cache_write_token_price for 1-hour writes. |
| $ai_request_price | (Optional) Price per request |
| $ai_request_count | (Optional) Number of requests (defaults to 1 if $ai_request_price is set) |
| $ai_web_search_price | (Optional) Price per web search |
| $ai_web_search_count | (Optional) Number of web searches performed |

### Cache properties

| Property | Description |
| --- | --- |
| $ai_cache_read_input_tokens | (Optional) Number of tokens read from cache |
| $ai_cache_creation_input_tokens | (Optional) Number of tokens written to cache (Anthropic-specific)When both TTL-specific counts are present, PostHog uses them instead of this aggregate. The aggregate should equal their sum; if either count is missing, PostHog uses the aggregate. |
| $ai_cache_creation_5m_input_tokens | (Optional) Number of tokens written to Anthropic's 5-minute cache |
| $ai_cache_creation_1h_input_tokens | (Optional) Number of tokens written to Anthropic's 1-hour cache |
| $ai_cache_reporting_exclusive | (Optional) Whether cache tokens are excluded from $ai_input_tokens. When true, cache tokens are separate from input tokens. When false, input tokens already include cache tokens. Defaults to true for Anthropic provider or Claude models, false otherwise. |

### Model parameters

| Property | Description |
| --- | --- |
| $ai_temperature | (Optional) Temperature parameter used in the LLM request |
| $ai_stream | (Optional) Whether the response was streamed |
| $ai_max_tokens | (Optional) Maximum tokens setting for the LLM response |

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better