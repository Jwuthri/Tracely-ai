> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# LangChain AI Observability installation - Docs

Copy page

# LangChain AI Observability installation - Docs

![](https://res.cloudinary.com/dmukukwp6/image/upload/texture_tan_9608fcca70)

![](https://res.cloudinary.com/dmukukwp6/image/upload/texture_tan_dark_a92b0e022d)

Let AI instrument your LLM calls for you

Skip the manual setup — run this in your project and the wizard installs the SDK and wires up AI Observability for you.

`npx @posthog/wizard ai-observability`

[Learn more](/wizard.md)

![PostHog Wizard hedgehog](https://res.cloudinary.com/dmukukwp6/image/upload/wizard_3f8bb7a240.png)

![](https://res.cloudinary.com/dmukukwp6/image/upload/wizard_3f8bb7a240.png)Let AI instrument your LLM calls for you

1.  1

    ## Install dependencies

    Required

    **Full working examples**

    See the complete [Node.js](https://github.com/PostHog/posthog-js/tree/main/examples/example-ai-langchain) and [Python](https://github.com/PostHog/posthog-python/tree/master/examples/example-ai-langchain) examples on GitHub.

    Install the PostHog SDK and LangChain with OpenAI.

    PostHog AI

    ### Python

    ```bash
    pip install posthog "langchain>=1.0" langchain-core langchain-openai
    ```

    ### Node

    ```bash
    npm install posthog-node @posthog/ai langchain@^1.0 @langchain/core @langchain/openai zod
    ```

2.  2

    ## Configure PostHog

    Required

    Create a PostHog client once, then build a callback handler for each request or conversation. `distinct_id` ties each call to a user, and `$ai_session_id` groups calls in one conversation.

    PostHog AI

    ### Python

    ```python
    from posthog import Posthog
    from posthog.ai.langchain import CallbackHandler
    posthog = Posthog("<ph_project_token>", host="https://us.i.posthog.com")
    def create_handler(user_id: str, session_id: str) -> CallbackHandler:
        return CallbackHandler(
            client=posthog,
            distinct_id=user_id,
            properties={"$ai_session_id": session_id},
        )
    ```

    ### Node

    ```typescript
    import { PostHog } from 'posthog-node'
    import { LangChainCallbackHandler } from '@posthog/ai/langchain'
    const posthog = new PostHog('<ph_project_token>', { host: 'https://us.i.posthog.com' })
    function createHandler(userId: string, sessionId: string): LangChainCallbackHandler {
      return new LangChainCallbackHandler({
        client: posthog,
        distinctId: userId,
        properties: { $ai_session_id: sessionId },
      })
    }
    ```

    > **Note:** If you want to capture LLM events anonymously, omit `distinct_id`/`distinctId` when constructing the handler. See our docs on [anonymous vs identified events](/docs/data/anonymous-vs-identified-events.md) to learn more.

3.  3

    ## Call LangChain

    Required

    Build your agent once and build the handler on each turn to track the relevant sessions. Traces, generations, and spans (tool calls) are automatically captured.

    PostHog AI

    ### Python

    ```python
    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from langchain.agents import create_agent
    @tool
    def get_weather(city: str) -> str:
        """Get the weather for a given city."""
        return f"It's always sunny in {city}!"
    model = ChatOpenAI(openai_api_key="your_openai_api_key")
    agent = create_agent(model, tools=[get_weather])
    def ask(user_input: str, user_id: str, conversation_id: str) -> str:
        handler = create_handler(user_id=user_id, session_id=conversation_id)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={"callbacks": [handler]},
        )
        return result["messages"][-1].content
    ask("What's the weather in Paris?", "user_123", "conversation-abc")
    ```

    ### Node

    ```typescript
    import { ChatOpenAI } from '@langchain/openai'
    import { tool } from '@langchain/core/tools'
    import { createAgent } from 'langchain'
    import { z } from 'zod'
    const getWeather = tool(
      (input) => `It's always sunny in ${input.city}!`,
      {
        name: 'get_weather',
        description: 'Get the weather for a given city',
        schema: z.object({
          city: z.string().describe('The city to get the weather for'),
        }),
      }
    )
    const model = new ChatOpenAI({ apiKey: 'your_openai_api_key' })
    const agent = createAgent({ model, tools: [getWeather] })
    async function ask(userInput: string, userId: string, conversationId: string): Promise<string> {
      const handler = createHandler(userId, conversationId)
      const result = await agent.invoke(
        { messages: [{ role: 'user', content: userInput }] },
        { callbacks: [handler] }
      )
      return result.messages[result.messages.length - 1].content
    }
    await ask("What's the weather in Paris?", 'user_123', 'conversation-abc')
    ```

    > **Using LangChain 0.x?** LangChain built agents with `AgentExecutor` before 1.0. Everything else on this page is the same on either version, including the handler and the properties it sets. See LangChain's [migration guide](https://docs.langchain.com/oss/python/migrate/langchain-v1) to move to `create_agent`.

    PostHog automatically captures an `$ai_generation` event along with these properties:

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

    The handler also builds a trace hierarchy automatically based on how you structure your agent. Pass the same `$ai_session_id` to every handler you construct for a conversation, to group its calls into one session. Pass `trace_id`/`traceId` too, to control the top-level trace ID instead of letting PostHog generate one.

4.  ## Verify traces and generations

    Recommended

    *Confirm LLM events are being sent to PostHog*

    Let's make sure LLM events are being captured and sent to PostHog. Under **AI Observability**, you should see rows of data appear in the **Traces** and **Generations** tabs.

    ![LLM generations in PostHog](https://res.cloudinary.com/dmukukwp6/image/upload/SCR_20250807_syne_ecd0801880.png)![LLM generations in PostHog](https://res.cloudinary.com/dmukukwp6/image/upload/SCR_20250807_syjm_5baab36590.png)

    [Check for LLM events in PostHog](https://app.posthog.com/ai-observability/generations)

5.  4

    ## Next steps

    Recommended

    Now that you're capturing AI conversations, continue with the resources below to learn what else AI Observability enables within the PostHog platform.

    | Resource | Description |
    | --- | --- |
    | [Basics](/docs/ai-observability/basics.md) | Learn the basics of how LLM calls become events in PostHog. |
    | [Generations](/docs/ai-observability/generations.md) | Read about the $ai_generation event and its properties. |
    | [Traces](/docs/ai-observability/traces.md) | Explore the trace hierarchy and how to use it to debug LLM calls. |
    | [Spans](/docs/ai-observability/spans.md) | Review spans and their role in representing individual operations. |
    | [Anaylze LLM performance](/docs/ai-observability/dashboard.md) | Learn how to create dashboards to analyze LLM performance. |

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better