---
title: AI Observability Setup - Instrument
description: Swap in the wrapper client, then attach identity and tool spans so the calls form a session tree
---

The install doc holds the code for this variant. Copy it and change the values. This step covers what the doc cannot know: the values this app supplies, and the shape the result must have.

## Swap the client

Build a PostHog client. Replace the vendor client with the PostHog wrapper for this provider. The wrapper takes the same constructor arguments and stays call-compatible, so the existing calls keep working. A gateway keeps its `base_url`.

Keep the setup at module level, next to the existing client. Under ten lines is normal.

- Do not wrap it in an init function.
- Do not add module globals.
- Do not add a presence check that raises. `os.environ["POSTHOG_API_KEY"]` already fails loudly when the key is unset.

Route the token and host through env vars with `set_env_values`. Reuse the names the project already uses. Add the names to `.env.example` with empty values. Never write a real key to a file. `.env.example` is documentation and must stay committed — if you edit `.gitignore`, ignore only `.env`, never `.env.example`.

Agent frameworks use their own tracing hook in place of a wrapper. Take it from the install doc. Do not substitute an OTel instrumentor.

## Attach identity to every call

Three per-call parameters carry the tree. Node uses camelCase.

| Parameter | Holds | Cardinality |
|---|---|---|
| `posthog_properties` with `$ai_session_id` | the conversation | one id for the whole conversation |
| `posthog_trace_id` | the turn | a new id per turn |
| `posthog_distinct_id` | the user | the person |

Every call inside one turn takes the same `posthog_trace_id`. If you omit it, the wrapper mints a fresh id per call, and each generation lands in its own trace.

**Cardinality is what gets graded.** One conversation is one session id. One turn is one trace id. An id minted per call looks instrumented and groups nothing.

Every app gets a session id. If the app has no conversation field, the process run is the session. Do not skip the step.

If the app has no user id, leave `posthog_distinct_id` out. Anonymous is a finding for the report. Do not invent an id.

`$ai_session_id` accepts letters, numbers, and `- _ ~ . @ ( ) ! ' : |`. A raw thread id with a slash or a hash fails. Check the value before you pass it through.

### A gateway must name its provider

The OpenAI wrapper reports `openai` whatever host it calls. PostHog prices tokens by `$ai_model` and `$ai_provider`. A gateway call at the default gets the wrong price. Send the real provider:

```python
posthog_properties={"$ai_session_id": session_id, "$ai_provider": "groq"}
```

## Capture tool calls as spans

The wrapper records the model call. It never sees the tool dispatch loop, so nothing else records a tool run.

If the app registers tools, capture each run as an `$ai_span` event with `posthog.capture()`. Give it the turn's `$ai_trace_id` so the span joins the trace. The install doc lists the span properties.

Put the capture next to the existing dispatch. Do not restructure the tool loop.

Agent frameworks and the Vercel AI SDK emit tool spans on their own. Add nothing on those variants.

An app that registers no tools has no spans. That is a complete result, not a gap.

## Do not

- Do not restructure the app. This step swaps a constructor and adds arguments to calls.
- Do not omit `posthog_trace_id` and expect the calls to group.
- Do not mint a session id per call or per turn.
- Do not leave a gateway reporting `$ai_provider` as `openai`.
- Do not add spans when the app registers no tools.
- Do not ship code whose imports fail. Go back to `1-begin.md` and pick another variant.

---

**Upon completion, continue with:** [4-verify.md](4-verify.md)