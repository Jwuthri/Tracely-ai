"""Centralized settings (pydantic-settings), read from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tracely_env: str = "dev"

    # Postgres
    database_url: str = "postgresql+asyncpg://tracely:tracely@localhost:5432/tracely"
    alembic_database_url: str = "postgresql+psycopg://tracely:tracely@localhost:5432/tracely"

    # ClickHouse
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "tracely"
    clickhouse_async_insert: int = 1

    # Redis (Celery)
    redis_url: str = "redis://localhost:6379/0"
    # Celery worker pool. `solo` (default for local dev) avoids fork issues with native libs
    # (numba/UMAP/HDBSCAN used by failure intelligence). Prod uses `prefork` so multiple workers
    # process tasks in parallel; switch to `threads` if a numba/UMAP fork bug resurfaces. The
    # docker-compose worker reads these at startup — `prefork` here makes the worker pick up the
    # CONCURRENCY too.
    celery_pool: str = "solo"
    celery_concurrency: int = 1
    # The queue name the API produces onto and the worker consumes (celery_app's
    # `task_default_queue`). Named here so the ops self-check measures the RIGHT list.
    celery_queue: str = "ingestion"
    # Where the deployment's own health alerts go (Slack incoming-webhook URL or any webhook
    # endpoint). Blank = log only; the beat self-check still runs and still logs `selfcheck`.
    ops_alert_webhook: str = ""

    # S3 / MinIO
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "tracely"
    s3_secret_access_key: str = "tracely-secret"
    s3_bucket: str = "tracely-events"
    s3_event_prefix: str = "events/"

    ingestion_delay_seconds: int = 0
    # Max accepted OTLP /v1/traces body size. The endpoint reads the whole body into memory before
    # offloading, so an unbounded post (or a flood of large ones) can exhaust backend memory. Bodies
    # over this many bytes are rejected with 413. Default 16 MiB (well above a normal OTLP batch).
    max_ingest_bytes: int = 16 * 1024 * 1024
    # Spans/traces with no agent are attributed to this fallback agent slug, so agent-scoped
    # features (failure clusters, CI gates) still apply to plain LLM calls. Set to "" to disable
    # and leave agent-less traces unattributed.
    default_agent_slug: str = "default"

    # online evaluation
    eval_latency_budget_ms: int = 60000
    # Trailing debounce for eval-on-ingest: a trace whose spans arrive in several OTLP batches is
    # evaluated once, this many seconds after the last batch (not once per batch). See
    # infrastructure/queue/eval_debounce.py.
    eval_debounce_seconds: int = 4
    # LLM access — every chat call goes through LangChain `create_agent` against OpenRouter
    # (see infrastructure/llm/provider.py). Model ids are OpenRouter-style `provider/model`.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Symmetric key (any string, >=32 chars — SHA256-derived into a Fernet key, same UX as
    # SESSION_SECRET) encrypting a workspace's own OpenRouter key at rest in Postgres
    # (`Project.openrouter_api_key_encrypted`). Only needed once a workspace sets its own key —
    # blank deployments (server-wide key only) are unaffected.
    secrets_encryption_key: str = ""
    # Multi-tenant signup (AUTH_MODE=local). Off by default: a self-hosted deployment is claimed
    # by its first registrant and everyone else arrives by invite, so an exposed URL can't be
    # used to mint accounts. Hosted cloud turns it on — each signup gets its own personal
    # organization and workspace.
    allow_public_signup: bool = False
    # Whether a workspace may point Tracely's outbound HTTP (agent endpoints, monitor webhooks) at
    # loopback/private/link-local addresses. Unset = allowed everywhere except prod, where the
    # worker shares a network with the datastores (see `infrastructure/net.py`).
    allow_private_urls: bool | None = None
    # Populate every new workspace with the demo dataset (traces, clusters, cases, gates) so it
    # opens on a working product instead of empty pages. Best-effort and detached — it never
    # blocks or fails workspace creation. Turn off for deployments that want clean workspaces.
    seed_new_workspaces: bool = True
    # Hosted-cloud hard gate: when true, the server-wide LLM/embedding credentials
    # (OPENROUTER_API_KEY / LLM_JUDGE_API_KEY / OPENAI_API_KEY) never apply to ANY call — scoped
    # or not. Every AI feature then runs exclusively on the workspace's own OpenRouter key
    # (Settings → OpenRouter key), and a future code path that forgets `use_project_key()` fails
    # closed (no LLM) instead of silently billing the operator. Self-host default: off, so a
    # single-tenant deployment keeps using its server-wide keys exactly as before.
    require_project_llm_key: bool = False

    # ── hosted-cloud billing (all off by default — self-hosters are unaffected) ──
    # Master switch: monthly trace quota enforcement + the Stripe endpoints + the billing UI.
    billing_enabled: bool = False
    # Monthly ingested-trace caps per plan. Counted per project, per UTC calendar month, over
    # externally-POSTed OTLP traces only (Tracely's own recordings and emulated scenario turns
    # never count). `unlimited` plan = no cap.
    free_trace_limit: int = 20_000
    pro_trace_limit: int = 1_000_000
    # Per-organization caps on workspaces and seats (members + pending invites). Personal
    # accounts are always 1/1 — these apply to company orgs. Only enforced when billing is on, so
    # a self-hosted deployment stays uncapped.
    free_workspace_limit: int = 3
    pro_workspace_limit: int = 10
    free_seat_limit: int = 3
    pro_seat_limit: int = 10
    # How many COMPANY organizations one user may BELONG to (any role). One, and no plan lifts
    # it: an org is a quota pool, so extra orgs would be extra free quota, and counting
    # membership rather than ownership stops someone inside a company running a second one on
    # the side. Personal accounts don't count — everyone gets exactly one of those too.
    max_organizations_per_user: int = 1
    # Stripe (subscription billing). Secret key + the Pro plan's monthly price id, plus the
    # webhook signing secret — required whenever the secret key is set, because an unverified
    # webhook endpoint would let anyone flip a workspace's plan with a curl.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""

    # Cap on OUTPUT tokens per judge/agent call. A judge emits a small structured verdict, so the
    # providers' 64k default is pure waste — and worse, OpenRouter reserves credit for the full
    # max_tokens UP FRONT, so an uncapped request fails with "requires more credits, you requested
    # up to 65536 tokens" while the balance is nowhere near spent. That surfaces as an opaque
    # `TypeError: 'NoneType' object is not iterable` (OpenRouter answers HTTP 200 with an error
    # body and no `choices`, which the OpenAI SDK's parser can't read). Raise this if a
    # reasoning-heavy judge model starts getting truncated.
    llm_max_output_tokens: int = 4096
    # judge model + legacy direct-endpoint fallback (used only when no OpenRouter key is set,
    # so existing deployments keep their judge until they switch keys)
    llm_judge_model: str = "openai/gpt-5.4-nano"
    llm_judge_api_key: str = ""
    llm_judge_base_url: str = "https://api.openai.com/v1"

    # AI Observability (PostHog). Every LLM call funnels through infrastructure/llm/provider.py,
    # so that is the one place a `posthog.ai.langchain.CallbackHandler` is attached — sessions,
    # traces, generations and tool spans for the judge, the assistant, and scenario simulation.
    # Blank key disables it exactly like every other optional credential here (OPENROUTER_API_KEY,
    # RESEND_API_KEY, SENTRY_DSN): no capture, nothing to configure for a deployment that opts out.
    posthog_api_key: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    # The in-app assistant (the dashboard's chat widget). Unlike every other LLM call, this one
    # runs on OUR OpenRouter key — it answers questions about Tracely, so it must work in a
    # workspace that has configured no key of its own (`provider.use_server_key`). A Gemini flash
    # model: a 1M context, and image input for when the assistant learns to look at the user's
    # screen. Watch the axis when changing this — `flash-lite` is cheaper on INPUT and dearer on
    # OUTPUT than `3.7-flash`, so it only wins while replies stay under ~12% of the prompt, and
    # `reasoning_effort` bills at the completion rate on the wrong side of that trade.
    assistant_model: str = "google/gemini-3.5-flash-lite"
    # "" disables thinking; otherwise minimal|low|medium|high. Medium costs latency and reasoning
    # tokens on every turn — drop to "low" if replies feel slow for what they are.
    assistant_reasoning_effort: str = "medium"
    # The assistant is an agent with ~30 tools, and every tool schema rides along on EVERY model
    # call of a tool loop — the dominant cost of a turn, paid several times over. So a cheap
    # model picks the handful of tools a question actually needs first
    # (`LLMToolSelectorMiddleware`), and the expensive model only ever sees those. One extra
    # small call buys a much smaller prompt on all the calls that follow.
    assistant_tool_selector_model: str = "openai/gpt-oss-120b"
    assistant_max_tools: int = 8  # per turn, after selection; 0 disables selection entirely
    # Runaway guard: a model that keeps calling tools without concluding. `end` stops the loop
    # and lets it answer with what it has, rather than erroring the turn away.
    assistant_max_model_calls: int = 12
    # What one CONVERSATION may spend of our credit, in USD. Cumulative across its turns — at
    # roughly half a cent a turn this is ~200 turns, so it catches a loop, not a heavy user.
    # Enforced before a turn starts AND mid-stream, because one turn's tool loop can run away
    # on its own. Must be float dollars: `estimate_cost_usd_cents` rounds a normal turn to 0.
    assistant_budget_usd: float = 1.0

    # failure intelligence (embeddings + agents) — embeddings ride the OpenRouter key too
    # (OpenAI-compatible /embeddings); this key is only the direct-OpenAI fallback.
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1024
    agent_model: str = "gpt-5.4-mini"
    # cluster directly on cosine distance below this many failures; UMAP-denoise at/above it
    # (UMAP needs a large, diverse set — on few/near-duplicate points it invents structure).
    fi_umap_min_n: int = 50
    fi_min_cluster_size: int = 2
    # Structural (ingest-time) clustering: a new failure joins an existing cluster of the same
    # agent when it has the same failed evaluators and this much token overlap. Higher = more,
    # tighter clusters; >1 disables merging (exact signature hash only, the old behaviour).
    cluster_merge_similarity: float = 0.5
    # Default floor for GET /api/clusters — one-off failures are noise, not an issue. Callers
    # override per request with ?min_size=N (N >= 2).
    cluster_min_size: int = 5

    # Meta-analysis ("Analyze"): cross-metric correlation/outlier synthesis over an agent's
    # evaluator scores. The stats (Spearman, z-score) are deterministic Python; only the prose
    # synthesis is an LLM call — a slightly larger model than the per-cell judge is worth it here
    # since it reasons over the whole metric set at once. Goes through provider.run_structured_agent.
    meta_analysis_model: str = "openai/gpt-5-mini"

    # Rolling summary: the per-span accumulating conversation summary that backs @HISTORY / the
    # conversation judge (compressed history instead of the raw transcript). A step whose components
    # fit under `rolling_summary_step_max_tokens` is kept VERBATIM (no LLM, no information loss);
    # only larger steps are summarized by the model. The summarizer goes through the provider seam.
    rolling_summary_model: str = "openai/gpt-5.4-nano"
    rolling_summary_step_max_tokens: int = 512
    # Whole-summary budget: when the accumulated summary exceeds this many tokens, the older items
    # (everything but the last 2, which stay verbatim) are recursively compacted into one block.
    rolling_summary_max_tokens: int = 20000

    # CI/CD gate soft thresholds — latency/token deltas vs the baseline (last green gate) raise a
    # WARNING by default (not a hard fail); fail-to-pass stays the only blocking gate unless the
    # block flag is set. Percentages are "% worse than baseline".
    gate_latency_warn_pct: float = 25.0
    gate_tokens_warn_pct: float = 25.0
    gate_block_on_warnings: bool = False
    # Coverage policy. A gate that exercised NONE of its promoted cases (every case SKIPped
    # because CI emitted no matching trace) is ALWAYS treated as a non-PASS `NO_COVERAGE` —
    # a green gate that tested nothing is the worst failure mode for a merge-blocker. When this
    # flag is on, ANY skipped case (partial coverage) also blocks; off (default) only all-skip blocks.
    gate_require_full_coverage: bool = False
    # Judge-in-the-gate. Cases promoted from an answer-QUALITY failure (a hallucination / wrong
    # answer with a structurally-clean trace) carry a `quality` assertion: at gate time the answer
    # judge re-grades the replayed answer and a sub-threshold score FAILs the case. This is what
    # turns the gate from "catches crashes" into "catches the bad answers customers fear". Set
    # False to record the quality verdict but keep it advisory (don't block) if the judge is noisy.
    gate_quality_blocks: bool = True
    # The canonical answer-quality judge the gate enforces (an AGENT_RUN llm_judge score_name).
    # Scoped to ONE judge on purpose: "block when the answer is wrong/unfaithful", not "block when
    # any AGENT_RUN judge — including signal detectors with inverted thresholds — fires". Blank =
    # consider every enabled AGENT_RUN llm_judge (legacy/broad behavior).
    gate_quality_score_name: str = "tracely.run.quality"
    # Emulated conversations. Fraction of scenarios that must PASS for the scenario half of a gate
    # to be green. 1.0 = every conversation must pass (right for SCRIPTED regression suites, which
    # are tests). Adversarial suites want it lower — an attack suite of any size will land a few,
    # and a gate demanding zero successful attacks forever is a gate people mute. A conversation
    # that ran but was never graded counts against the rate, never as a pass.
    gate_scenario_min_pass_rate: float = 1.0
    # Ceiling on how long a gate waits for the CUSTOMER's own spans to arrive on an emulated
    # conversation before grading it. Tracely's own turn spans are written inline, so this only
    # covers spans their service exports after honouring our `traceparent`; the wait ends as soon
    # as the span count stops growing, and this is just the cap.
    gate_scenario_span_grace_s: int = 20
    # Adversarial scenarios. Blank = the server's default judge model (`llm_judge_model`). The
    # attacker wants a model that will actually play the role — a heavily safety-tuned one refuses
    # to red-team and the suite quietly finds nothing — so it is worth pinning separately from the
    # judge, which only needs to read a transcript honestly.
    attacker_model: str = ""
    # How many of the agent's known production failure clusters to hand the attacker as leverage.
    # 0 disables it. This is the Tracely-native part: the attacker probes where this agent has
    # actually broken before, which needs the failure history no other red-team tool has.
    # The judge that decides whether the attack worked. Blank = default judge model. Kept separate
    # from `attacker_model`: the attacker needs a model willing to role-play, the judge needs one
    # that reads a transcript strictly.
    attacker_judge_model: str = ""
    attacker_weakness_hints: int = 5

    # ── Internal-run recording ────────────────────────────────────────────────────
    # Record what Tracely itself did — the judge's prompt and reply, the attacker's move, the
    # call to the customer's endpoint — as a trace, so "why did this evaluator say that?" is
    # answerable in the UI instead of in worker logs. Hidden from every list; fetched by the
    # "Show eval" toggle. Costs one extra (small) trace per evaluation, so it is switchable for
    # high-volume projects.
    introspection_enabled: bool = True

    # Durable judge conversations: a SEQUENTIAL evaluator holds one chat with the model (rubric →
    # item → verdict → item …), checkpointed in Postgres so turn 6 resumes what turn 5 left. The
    # prefix is byte-identical between calls, so the provider serves it from its prompt cache.
    # Off ⇒ every item is a fresh one-shot question and the prior verdict is pasted in as text
    # (the pre-checkpoint behaviour), which is also the automatic fallback when Postgres is
    # unreachable. `infrastructure/llm/checkpointer.py`.
    eval_chat_enabled: bool = True
    # Connections the checkpointer pool may open. One per concurrent grading task is plenty — the
    # worker runs --pool=solo locally, and the API's on-demand run grades in a threadpool.
    eval_chat_pool_size: int = 8

    # Where a worker-side task reaches this API (the Data page's "Seed demo data" runs the seeder
    # there, and it drives the product through its own HTTP API). Container DNS in compose; the
    # host port when the worker runs on the host.
    internal_api_url: str = "http://backend:8000"

    # ── Auth & multi-tenancy ──────────────────────────────────────────────────────
    # "dev"   = no human auth; the ingest key is the only credential (today's behavior, no secret needed).
    # "local" = email/password owned by this backend; POST /auth/login issues an HS256 session JWT.
    # "clerk" = Clerk owns login/orgs/invites; this backend verifies Clerk's RS256 session JWT.
    auth_mode: str = "dev"
    # HS256 signing key for local-mode session JWTs. REQUIRED (>=32 chars) when auth_mode="local".
    session_secret: str = ""
    session_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days
    session_issuer: str = "tracely"
    # Password-reset links. Deliberately far shorter-lived than an invite (7 days): a reset link is
    # a full account takeover if intercepted, and the user is expected to click it immediately.
    password_reset_ttl_seconds: int = 60 * 60  # 1 hour
    # Clerk (auth_mode="clerk"): the issuer / Frontend API origin, e.g. https://<slug>.clerk.accounts.dev
    clerk_issuer: str = ""
    clerk_jwks_url: str = ""  # blank → derived as f"{clerk_issuer}/.well-known/jwks.json"
    clerk_audience: str = ""  # optional 'aud' to pin; blank → not enforced
    clerk_secret_key: str = ""
    clerk_jwks_cache_seconds: int = 600
    # Hosted CORS: the browser-facing frontend origin allowed to call the API directly (blank → localhost only).
    frontend_origin: str = ""
    # Sentry: optional. Blank disables it; non-blank + `sentry-sdk` installed activates error capture.
    sentry_dsn: str = ""
    sentry_environment: str = ""  # default = tracely_env if blank

    # ── Transactional email (Resend) ──────────────────────────────────────────────
    # Optional. When RESEND_API_KEY is set, team invites are emailed automatically; when blank the
    # invite link is only surfaced once in the UI for manual sharing (dev default — no email sent).
    resend_api_key: str = ""
    # Sender identity for invite emails. The default uses Resend's shared test domain, which only
    # delivers to your OWN Resend-account email — verify a domain (resend.com/domains) and set this to
    # e.g. "Tracely <invites@yourdomain.com>" to reach real teammates.
    email_from: str = "Tracely <onboarding@resend.dev>"
    # Public base URL of the frontend; used to build the accept-invite link inside invite emails.
    app_base_url: str = "http://localhost:3001"

    @field_validator("allow_private_urls", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """An empty string means "not configured", not "invalid".

        This setting is deliberately tri-state (`None` = allowed outside prod), and every way of
        passing env vars through a template renders an unset variable as `""` — the compose
        `x-app-env` anchor (`${ALLOW_PRIVATE_URLS:-}`) and a blank Railway variable both do. Without
        this, one unset optional flag stops the whole process from booting.
        """
        return None if isinstance(v, str) and v.strip() == "" else v

    @model_validator(mode="after")
    def _validate_auth(self) -> "Settings":
        if self.auth_mode not in ("dev", "local", "clerk"):
            raise ValueError(f"AUTH_MODE must be dev|local|clerk, got {self.auth_mode!r}")
        if self.auth_mode == "local" and len(self.session_secret) < 32:
            raise ValueError("AUTH_MODE=local requires SESSION_SECRET (>=32 chars)")
        if self.auth_mode == "clerk" and not self.clerk_issuer:
            raise ValueError("AUTH_MODE=clerk requires CLERK_ISSUER")
        # Prod refuses dev-mode auth: dev mode means "no human auth" and the seeded `tracely_dev_key`
        # is a valid ingest credential, so booting prod in dev mode is world-pwnable. Fail fast at
        # startup instead of shipping an open backend. (Set TRACELY_ENV=prod only in real prod.)
        if self.is_prod and self.auth_mode == "dev":
            raise ValueError(
                "TRACELY_ENV=prod requires AUTH_MODE=local or clerk (dev mode has no human auth)"
            )
        # A Stripe key without the webhook signing secret means the /api/billing/webhook endpoint
        # cannot verify senders — anyone could POST a forged `checkout.session.completed` and
        # upgrade themselves. Refuse to boot rather than run open.
        if self.stripe_secret_key and not self.stripe_webhook_secret:
            raise ValueError("STRIPE_SECRET_KEY is set but STRIPE_WEBHOOK_SECRET is empty")
        return self

    @property
    def is_prod(self) -> bool:
        return self.tracely_env.lower() in ("prod", "production")

    @property
    def private_urls_allowed(self) -> bool:
        return (not self.is_prod) if self.allow_private_urls is None else self.allow_private_urls

    @property
    def resolved_clerk_jwks_url(self) -> str:
        if self.clerk_jwks_url:
            return self.clerk_jwks_url
        return f"{self.clerk_issuer.rstrip('/')}/.well-known/jwks.json" if self.clerk_issuer else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
