# Framework rules

Follow these when integrating PostHog into this framework.

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
