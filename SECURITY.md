# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's
[private vulnerability reporting](https://github.com/Jwuthri/Tracely-ai/security/advisories/new).
Include what the issue is, how to reproduce it, and what you think the impact is.

You can expect an acknowledgement within 72 hours and a fix or mitigation plan within 14 days
for confirmed issues. We'll credit you in the advisory unless you prefer otherwise.

## Scope

Things we consider in scope:

- Cross-tenant data access (anything that lets one workspace read or change another's traces,
  evaluators, cases, gates, keys, or chats)
- Authentication / session bypass (`AUTH_MODE=local` / `clerk`)
- Ingest-key leakage or privilege escalation (an ingest key should only read and write traces
  for its own project)
- Injection via ingested trace content (OTLP payloads, tool outputs) reaching SQL, the LLM
  judge's privileged context, or the dashboard as XSS
- Server-side LLM key exposure (the per-workspace key rule in `CLAUDE.md`)
- Vulnerabilities in the published SDK (`tracely-ai` on PyPI)

Out of scope: issues requiring `AUTH_MODE=dev` (open, local-only; prod refuses to boot in it),
the seeded `tracely_dev_key`, denial of service against a self-hosted instance, and reports from
automated scanners without a working proof of concept.

## Supported versions

Only the latest release on `master` and the latest published SDK receive security fixes.
