# Growth plan — directories, communities, tools

Companion to [SEO.md](SEO.md). Same premise, one line: **the category has no search demand yet**
(`llm regression testing` = 10/mo), so growth has to borrow demand from the categories that *do* —
LLM observability, evaluation, and "Langfuse/LangSmith alternative".

That single fact decides everything below. Directories are **not a traffic channel** for a dev infra
tool — nobody browses a tool directory looking for a CI gate. They are worth doing for exactly two
reasons:

1. **Backlinks** to a 3-month-old domain that has none. This is the real payoff and it compounds into SEO.md's plan.
2. **The alternative-to SERP.** `langfuse alternatives` already converts (SEO.md, Finding 2). Directory alternative-pages rank in it. Getting listed *is* the SEO play.

Judge every listing by "does this produce a dofollow link and/or an alternative-page slot". If not, skip it.

---

## 0. Assets you own that competitors can't list

This is the leverage. Braintrust and LangSmith are closed SaaS — locked out of most of Tier A. Tracely has:

| Asset | Unlocks |
|---|---|
| **MIT + complete self-host** | OpenAlternative, every open-source directory, `awesome-*` lists |
| **642 GitHub stars** | Awesome-list maintainers filter on stars; you clear most bars |
| **PyPI `tracely-ai`** | Package-ecosystem discovery (done — keep the README good) |
| **MCP server at `/mcp`** | MCP registries — a whole directory category most eval tools aren't in |
| **Agent skill (`skills/tracely`)** | Claude Code / agent-skill directories — brand new category, low competition |
| **Railway one-click template** | Railway template marketplace (organic installs, zero effort) |
| **Native OTLP** | The OpenTelemetry ecosystem registry + the CNCF OTel GenAI SIG |

Four of those seven are things pure-SaaS competitors literally cannot submit. Start there.

---

## 1. Tier A — do these first (all free, ~4 hours total)

Every item here costs $0. Verified 2026-08-15: OpenAlternative is no longer on this list — it went paid (see §3).

| # | Where | Why it matters | Effort |
|---|---|---|---|
| 1 | [AlternativeTo](https://alternativeto.net) | Domain authority ~80, free, community-edited. Add Tracely, then mark it an alternative to Langfuse, LangSmith, Braintrust, Arize Phoenix, Opik, Helicone, W&B Weave. Each mark = one more entry point. **Highest ROI free listing.** | 30 min |
| 2 | `awesome-*` PRs | ✅ **Submitted 2026-08-15, awaiting merge** — [tensorchord#755](https://github.com/tensorchord/Awesome-LLMOps/pull/755) (5.9k★, DCO green), [InftyAI#519](https://github.com/InftyAI/Awesome-LLMOps/pull/519) (README + `website/data.yml`), [awesome-llm-observability#12](https://github.com/ContextJet-ai/awesome-llm-observability/pull/12), [brandonhimpfen/awesome-llmops#40](https://github.com/brandonhimpfen/awesome-llmops/pull/40) — `awesomelistsio` now redirects there. Each discloses authorship; nudge with a polite comment if untouched in 2–3 weeks. | done |
| 3 | **GitHub topics** | You have 6; the cap is 20 and they're how awesome-list maintainers *and* GitHub search find you. Add: `observability`, `opentelemetry`, `otel`, `llm`, `agents`, `evaluation`, `tracing`, `llm-eval`, `ai-engineering`, `regression-testing`, `mcp`, `clickhouse`, `python`, `self-hosted`. **You have to do this — the `gh` CLI here is read-only on this repo.** | 5 min |
| 4 | **MCP registries** | You ship an MCP server and almost no eval tool does. Submit to [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers), Smithery, Glama, [mcp-registry](https://github.com/modelcontextprotocol/registry), `awesome-mcp-servers`. Needs a 3-line "Tracely MCP" section in the SDK docs to point at. | 1 hr |
| 5 | **Railway template gallery** | Template `n5n_LE` exists — confirm it's *published* to the public marketplace with screenshots + description, not just a private deploy link. Free installs from people browsing "observability". | 20 min |
| 6 | [OpenTelemetry registry](https://opentelemetry.io/ecosystem/registry/) | You are a genuine OTLP-native backend. Registry entry = a link from `opentelemetry.io`, which is about the strongest dev-infra backlink available for free. | 30 min |
| 7 | **GitHub Discussions + Releases** | 642 stars is your biggest owned audience. Turn on Discussions, and write real release notes — watchers get emailed for free. This outperforms every directory on this page. | 15 min |

---

## 2. Tier B — worth doing, lower payoff

- **devtools.sh**, **Console.dev** (curated dev-tool newsletter — pitch, don't submit; their audience is exactly right)
- **StackShare** — company/tool profile, decent DA
- `awesome-claude-code` / agent-skill directories — for the `skills/tracely` skill specifically
- **LibHunt / SaaSHub / Slant** — low quality, but free links; batch them into one 30-min session
- **Product Hunt (again)** — a *new* launch for a genuinely new thing (the agent skill, or the MCP server) is allowed and resets the spike. Don't relaunch the same product.

## 3. Skip — with reasons

| Skip | Why |
|---|---|
| **OpenAlternative** | Went paid: $97 with **no do-follow link**, $137 for one, $197/mo for placement. The free PR route is closed (CONTRIBUTING redirects to the paid form). The link was the whole point, so the real price is $137 — revisit only as a one-off after every free listing above is done. |
| **BetaList** | $39, 4–6 week queue, and its criteria favour *unreleased* products. You've already launched on PH and its audience is founders, not AI engineers. Wrong crowd, real money. |
| Generic AI-tool directories (PoweredByAI, "10,000+ AI tools", aiagentslist) | Consumer AI-tool traffic. Zero AI engineers. Mostly nofollow. |
| "Submit to 100 directories for $99" services | Spam-farm links; at best worthless, at worst a Google liability. |
| Paid AI directory listings | Never worth it for infra tooling. |

---

## 4. Paste-ready copy (write once, reuse 25×)

```
Name:        Tracely
URL:         https://tracely-ai.com
GitHub:      https://github.com/Jwuthri/Tracely-ai
Docs:        https://doc.tracely-ai.com
License:     MIT · self-hostable · Python SDK: pip install tracely-ai

Tagline (60):  Production agent failures become CI regression tests
Tagline (100): Trace-native CI/CD for AI agents — production failures become regression tests that block the PR

Short (250):
Tracely grades every agent trace as it lands, clusters the failures into issues, freezes the bad
runs into hermetic replayable cases, and blocks the pull request that would ship them again. No
hand-authored eval dataset — production already wrote it. Open source (MIT), self-hostable.

Long (600):
Every LLM eval tool asks you to hand-author a dataset: invent questions, write ideal answers, keep
them current. That dataset is a guess about what might break. Production already handed you the real
thing — a trace of the exact run that failed, with the exact input, tool calls and model responses.
Tracely is trace-native CI/CD for AI agents. Traces arrive over OTLP, evaluators grade them
automatically, failures cluster into issues, and any cluster can be frozen into a hermetic
regression case that replays in CI and blocks the PR that would reintroduce it. Open source under
MIT, self-host the whole stack (API, worker, UI, Postgres, ClickHouse, Redis, MinIO) in one click,
or use the hosted free tier at 20k traces/month.

Categories:  LLM observability · AI agent evaluation · LLMOps · Developer tools · Testing / CI
Tags:        llm-observability, llm-evaluation, ai-agents, evals, ci-cd, opentelemetry, tracing, self-hosted
Alternative to: Langfuse, LangSmith, Braintrust, Arize Phoenix, Opik, Helicone, W&B Weave, HoneyHive
Pricing:     Free self-host (MIT) · Free hosted 20k traces/mo · Team $49/mo
Screenshot:  .github/assets/dashboard.png
```

Keep this block identical everywhere. Consistent name + description across directories is what makes
Google treat them as corroborating entity signals rather than noise.

---

## 5. Communities — where the actual users are

The rule that matters: **you get one link per ten useful answers.** Communities can smell a launch
post. Pick three, not ten, and show up weekly.

| Community | Why this one | Your angle |
|---|---|---|
| **MLOps Community Slack** (`#llm-in-production`) | Highest concentration of people who own an agent in prod and are on the hook when it breaks. | Answer "how do you test agents?" questions. That's literally your product. |
| **OTel GenAI SIG / CNCF Slack** | You're OTLP-native. Nobody else in the eval space participates here. | Semantic-convention discussions; being the Python impl that follows them. |
| **r/LLMDevs, r/AI_Agents, r/mlops** | Reddit indexes in Google, so answers keep earning. | Comment answers, never link-drops. r/MachineLearning is stricter — read rules first. |
| **LangChain / LlamaIndex / CrewAI Discords** | Your SDK already auto-instruments these. | Help with tracing problems; the integration mention is natural there. |
| **X / LinkedIn** | You currently link **zero** socials from the landing page. | Post failure-cluster screenshots. "Here's a real agent failure and the test it became" is the whole pitch in one image. |

Two content moves that beat all directory work combined:

1. **A technical post on how the hermetic replay actually works** (span-level record/replay, deterministic ids). That's the HN-front-page shape — a mechanism, not a product. Your last HN went as a launch; this one goes as engineering.
2. **A "we ran N production agent failures through clustering, here's the taxonomy" data post.** You have the data nobody else does. Data posts get linked by other people writing about the category, which is how a new domain gets authority.

Fix first (5 minutes, blocks everything else): add X + GitHub + Discord links to the landing footer.
Every community conversation currently dead-ends at a page with no way to follow you.

---

## 6. Tools and budget

**Recommendation: spend $0/month on tools.** Free tiers cover everything at your size, and you're
already paying ~$10/mo for OpenSEO out of the $50–100.

| Need | Pick | Cost | Note |
|---|---|---|---|
| Email list | **MailerLite free** (1k subscribers, 12k sends/mo) | $0 | Brevo is fine too. Don't overthink; you can migrate a list of <1k in an hour. |
| Scheduling | **Buffer free** (3 channels, 10 queued posts) | $0 | Queue a week of posts on Sunday. |
| Analytics | Plausible/Umami self-hosted, or GSC only | $0 | You already self-host everything else. |

The honest note on email: **the tool isn't your bottleneck — you have no capture form anywhere on the
site and no list.** Adding MailerLite today gets you an empty dashboard. Do it in this order:

1. Ship an email field in the landing + docs footer ("Changelog, monthly, no spam") — one input, one API route.
2. Point it at MailerLite free.
3. Only send on releases. A changelog list for a dev tool with 642 stars is a genuinely good asset; a "marketing newsletter" is not.

Meanwhile you *already* have a mailing list you're not using: GitHub Releases emails every watcher, for free.

**What to do with the remaining ~$40–90/mo:** nothing recurring. At that level, sponsored newsletter
slots in AI-engineering land ($500+) are out of reach, and everything under $100 is a link farm.
Hold it and spend one-off. Best candidates, in order: (a) a `.dev`/`.com` domain if you're ever going
to move off `.xyz` — devs and directory editors do discount `.xyz`, and migrating is cheapest *now*
while the domain has no authority to lose; (b) a decent OG/demo video for the directory listings.

---

## 7. Two-week schedule

**Week 1 (~4 hrs)** — Tier A #3 topics (5 min) → #1 AlternativeTo + 7 alternative marks → #2 four awesome-list PRs → landing footer socials.

**Week 2 (~4 hrs)** — Tier A #4 MCP registries → #5 Railway gallery → #6 OTel registry → #7 Discussions + release notes → join the three communities and answer five questions each, no links.

**Then**: one technical post per fortnight, Tier B in one batch session when you're bored.

Measure only two things in Search Console: referring domains, and impressions on `langfuse
alternative`-family queries. Directory traffic itself will be near zero — that is expected and not a
failure.

---

## 8. How to actually submit — MCP registry + OTel registry

Both are PR/CLI flows, not web forms. Verified 2026-08-15.

### 8a. MCP registry (~30 min)

`server.json` is already written at the repo root. It declares the remote server at
`https://api.tracely-ai.com/mcp` (streamable HTTP — matches `api/mcp_server.py`).

Use **GitHub auth**, not DNS auth: DNS requires an apex TXT record and an Ed25519 key, and macOS
ships LibreSSL which can't generate one without `brew install openssl@3`. GitHub auth is an OAuth
prompt. The trade-off is the name — GitHub auth forces `io.github.jwuthri/tracely`; DNS auth would
let you own `com.tracely-ai/tracely`. Not worth the yak-shave now; you can republish under the
domain name later if it ever matters.

```bash
brew install mcp-publisher      # or: go install github.com/modelcontextprotocol/registry/cmd/publisher@latest
mcp-publisher login github      # opens an OAuth prompt; needs no repo scopes
mcp-publisher publish           # reads ./server.json
```

Preconditions: `https://api.tracely-ai.com/mcp` must be publicly reachable. It answers 401
without an `Authorization: Bearer <ingest-key>` header — that's fine and normal for remote servers,
but the host has to resolve and respond. Check before publishing.

Then the free aggregators, which mostly ingest the official registry or take a PR:
[Smithery](https://smithery.ai), [Glama](https://glama.ai/mcp/servers),
[punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers).

### 8b. OpenTelemetry registry (~30 min)

Fork [open-telemetry/opentelemetry.io](https://github.com/open-telemetry/opentelemetry.io), add one
file, open a PR. Naming convention is `<registryType>-<language>-<name>.yml`.

File: `data/registry/instrumentation-python-tracely.yml`

```yaml
title: Tracely
registryType: instrumentation
language: python
tags:
  - agents
  - llm
  - evaluation
  - observability
  - openai
  - langchain
urls:
  repo: https://github.com/Jwuthri/Tracely-ai
  website: https://tracely-ai.com
  docs: https://doc.tracely-ai.com
license: MIT
description:
  Auto-instruments AI agent frameworks and LLM SDKs (OpenAI, Anthropic, LangChain, LlamaIndex,
  CrewAI) and exports spans over OTLP. Traces are graded by evaluators, failures are clustered, and
  clusters can be replayed as regression tests in CI.
authors:
  - name: Julien Wuthrich
    url: https://github.com/Jwuthri
createdAt: 2026-08-15
isNative: false
isFirstParty: true
```

Notes on the choices: `registryType: instrumentation` because the registry lists components, not
vendors — the SDK is what plugs into OTel. `isFirstParty: true` (the instrumentation and the backend
come from the same project) and `isNative: false` (it is a library, not software with OTel built in).
The maintainers do review these, so the PR title should be `Add Tracely to the registry`.
