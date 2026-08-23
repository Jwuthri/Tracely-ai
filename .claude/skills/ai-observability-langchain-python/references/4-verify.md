---
title: AI Observability Setup - Verify
description: Give the user a way to trigger one turn, and grade the tree that reaches PostHog
---

Give the user a way to run one turn. Do not call the model yourself. You hold no credentials, and the user should watch the trace arrive.

Grade what reaches PostHog, not what the diff contains. A clean diff that produces one trace per call is a failed run.

## What correct looks like

Derive the expectation from the four facts in `1-begin.md`:

- One session holds the turns of one conversation.
- One trace holds every generation of one turn.
- A span appears for each tool run, if the app registers tools.
- The person matches the app's user id, if the app has one.
- `$ai_provider` names the real provider on a gateway app.

## Tell the user how to trigger it

Name the smallest path in the project that runs one turn. Prefer one that calls the model twice or uses a tool, so the tree has depth.

- A script: "Run `npm run <script>`."
- An API route: "Send `POST /api/chat` with a test message."
- A test that calls the model.

If no path exists, write one in the report as suggested code. Do not add it to the project unless the user asks.

Then open **LLM Analytics > Traces** in PostHog and open the newest trace. Check it against the list above. A second turn in the same conversation proves the session id groups the turns instead of splitting them.

Before you hand over, run the import the code depends on, such as `python3 -c "from posthog.ai.openai import OpenAI"`. If it fails, go back to `1-begin.md` and pick another variant.

## When it looks wrong

| Symptom | Cause |
|---|---|
| One trace per generation | `posthog_trace_id` is missing, or a new id goes to each call |
| A new session id on every trace | the session id comes from the wrong scope |
| No session id | `posthog_properties` never reaches the call |
| `$ai_provider` says `openai` on a gateway | the per-call override is missing |
| Anonymous person | `posthog_distinct_id` is missing |
| No tool spans | the `$ai_span` captures are missing or carry another trace id |
| Nothing arrives | the code still calls the vendor client, the key or host is unset, or a short script exits before the flush |

## Do not

- Do not run the vendor SDK.
- Do not put an API key in any file.
- Do not report a run as done while an import fails.
- Do not claim the setup works before the user confirms what landed. Report it as "wired, unverified" instead.