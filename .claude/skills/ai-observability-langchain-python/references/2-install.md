---
title: AI Observability Setup - Install
description: Declare the packages the variant needs, and no others
---

Declare the packages in the project manifest. Do not run the package manager. The build step installs them later.

Read the manifest first. If a package is already there, keep its version and say so in the report. Match the style of the entries around it.

## Providers and gateways

Every provider and every OpenAI-compatible gateway needs the PostHog SDK next to the vendor SDK.

| Runtime | Packages |
|---|---|
| Python | `posthog` |
| Node | `@posthog/ai`, `posthog-node` |

**Do not add OpenTelemetry packages.** The wrapper path needs none. If you reach for `opentelemetry-sdk`, `posthog[otel]`, or an `opentelemetry-instrumentation-*` package, you picked the wrong mechanism. Go back to `3-instrument.md`.

The vendor SDK is already in the manifest. Do not add or upgrade it.

Portkey also needs `portkey-ai`.

## Other variants

| Variant | Packages |
|---|---|
| Agent frameworks | The list the install doc names |
| `opentelemetry-*`, LlamaIndex, AWS Bedrock | The OTel packages the install doc names |
| `manual-capture` | `posthog` or `posthog-node` |

AWS Bedrock has no wrapper client. It instruments the AWS SDK through OpenTelemetry, so its package list differs from every other provider.

## Do not

- Do not run `npm install` or `pip install`.
- Do not edit the lockfile.
- Do not upgrade the vendor SDK.
- Do not add OpenTelemetry to a wrapper variant.

---

**Upon completion, continue with:** [3-instrument.md](3-instrument.md)