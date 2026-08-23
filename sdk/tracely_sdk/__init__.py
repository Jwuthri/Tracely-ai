"""Tracely SDK — automatic + manual tracing, exported to Tracely over OTLP.

Automatic (the default path — zero span code):

    import tracely_sdk as tracely
    tracely.init(env="prod")                       # activates OpenAI/Anthropic/… instrumentors

    with tracely.trace(agent="planner", conversation="conv-1", user="u_7"):
        OpenAI().chat.completions.create(model="gpt-4o", messages=[...])   # traced, no span code

    @tracely.observe(as_type="agent")              # function-level spans, auto-nested
    def plan(goal): ...

Manual (the escape hatch — full control):

    with tracely.agent("planner", version="v1") as a:
        with tracely.llm("gpt-4o") as g:
            tracely.set_io(g, input=prompt, output=completion)
            tracely.set_usage(g, input_tokens=812, output_tokens=96)
    tracely.flush()

Emits standard gen_ai.* / OpenInference-compatible attributes plus Tracely's first-class
`tracely.*` hints (agent id, version, run, conversation, turn, step, env) so the backend
populates the first-class span columns. Thin wrapper over the OpenTelemetry SDK: a custom
`SpanProcessor` stamps the active `tracely.trace()` context onto every span — including the
zero-touch provider spans created by the auto-instrumentors, which know nothing about Tracely.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from contextvars import ContextVar, copy_context
from typing import Any, Callable, Iterator

from opentelemetry import trace as otel_trace  # `trace` is our public run-context API (below)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from tracely_sdk.export import download_export, export_conversations, remember_connection

__all__ = [
    "init",
    "trace",
    "observe",
    "agent",
    "turn",
    "step",
    "llm",
    "tool",
    "thinking",
    "retriever",
    "embedding",
    "guardrail",
    "chain",
    "delegate",
    "skill",
    "set_io",
    "set_usage",
    "set_metadata",
    "set_agents",
    "set_state",
    "error",
    "flush",
    "run_in_thread",
    "fixtures",
    "fixture",
    "call_llm",
    "call_tool",
    "ToolError",
    "export_conversations",
    "download_export",
]

log = logging.getLogger("tracely")


class ToolError(RuntimeError):
    """Raised by call_tool/call_llm in hermetic replay when the recorded call errored — so the
    agent's own error handling (try/except) runs exactly as it would against the live tool."""


_tracer: otel_trace.Tracer | None = None
_provider: TracerProvider | None = None
_env: str = "prod"
# init(agent=…) / init(service_name=…): the agent name every span falls back to when the run
# context doesn't name one.
_agent: str = ""
# init(tenant=…): the tenant every run belongs to, for a process that serves exactly one.
_tenant: str = ""
# The `service_name` placeholder. A caller who chose a service name has named their agent; the
# untouched default has not — filing those under an agent literally called "agent" would be worse
# than the backend's `default`.
_DEFAULT_SERVICE_NAME = "agent"
_initialized: bool = False
# the active tracely.trace() run context (agent/conversation/turn/user/trace_name/env/metadata),
# stamped onto every span by TracelyContextSpanProcessor — including auto-instrumentor spans.
_run_ctx: ContextVar[dict | None] = ContextVar("tracely_run_ctx", default=None)
# recorded tool/LLM outputs for hermetic replay; set by `with fixtures(bundle): ...`
_fixtures: ContextVar[dict | None] = ContextVar("tracely_fixtures", default=None)


class TracelyContextSpanProcessor(SpanProcessor):
    """The linchpin (PRD §6, R4). Auto-instrumentor spans are created by *their* code and know
    nothing about Tracely — so on every span's `on_start` we read the active `tracely.trace()`
    context (a contextvar) and stamp `tracely.*` hints onto the span. That's how zero-touch
    provider spans inherit the run's agent/conversation/turn/user/env without the instrumentor
    knowing Tracely exists. Manual spans set the same attributes after start, so they win on
    conflict; `tracely.env` is owned here (run-ctx value, else the init() default)."""

    def on_start(self, span: Span, parent_context: Any = None) -> None:  # noqa: ARG002
        ctx = _run_ctx.get() or {}
        env = ctx.get("env") or _env
        if env:
            span.set_attribute("tracely.env", str(env))
        # The agent is never inferred backend-side, so `init(agent=…)` is what saves a whole app
        # from landing under `default` without naming the agent on every trace().
        agent = ctx.get("agent") or _agent
        if agent:
            span.set_attribute("tracely.agent.id", str(agent))
        # The tenant is the conversation's identity — the Agent Tracely gates, clusters and tests —
        # while `agent` above stays the per-span label. Both go on every span so a framework's own
        # spans carry them too.
        tenant = ctx.get("tenant") or _tenant
        if tenant:
            span.set_attribute("tracely.tenant.id", str(tenant))
        if not ctx:
            return
        if ctx.get("conversation"):
            span.set_attribute("tracely.conversation.id", str(ctx["conversation"]))
            span.set_attribute("session.id", str(ctx["conversation"]))
        if ctx.get("turn") is not None:
            span.set_attribute("tracely.turn.index", int(ctx["turn"]))
        if ctx.get("turn_id"):
            span.set_attribute("tracely.turn.id", str(ctx["turn_id"]))
        if ctx.get("user"):
            span.set_attribute("tracely.user.id", str(ctx["user"]))
        if ctx.get("trace_name"):
            span.set_attribute("tracely.trace.name", str(ctx["trace_name"]))
        for k, v in (ctx.get("metadata") or {}).items():
            if v is not None:
                span.set_attribute(
                    f"tracely.metadata.{k}",
                    v if isinstance(v, (str, int, float, bool)) else json.dumps(v, default=str),
                )
        # Conversation agent catalog (declared by the user) — stamped as one JSON attribute. The
        # backend extracts it per conversation (and strips it before ClickHouse) for the Agents
        # panel + @LIST_AGENT. Typically set once on the first turn; the redundancy is harmless.
        agents = ctx.get("agents")
        if agents:
            span.set_attribute("tracely.agents", json.dumps(agents, default=str))

    def on_end(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True


# ── redaction (PII / sensitive content) ──────────────────────────────────────
# Scrub on the EXPORT path: every span — manual or auto-instrumentor — passes through the exporter,
# so this is the one place that covers prompts/completions/tool args captured by zero-touch
# instrumentors (which never call set_io). Off by default; opt in via init(redact=...).

# Conservative built-in patterns for init(redact=True). Deliberately high-precision (few false
# positives) rather than exhaustive — pass your own patterns/callable for stricter policies.
_DEFAULT_PII_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),  # credit-card-shaped digit run
    re.compile(r"\b(?:\+?\d{1,2}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"),  # phone
]

_REDACTED = "[REDACTED]"


def _build_redactor(
    redact: bool | list[str] | Callable[[str, str], str] | None,
) -> Callable[[str, str], str] | None:
    """Resolve the `redact` argument into a `(attr_key, value) -> value` function, or None (off)."""
    if not redact:
        return None
    if callable(redact):
        return redact
    patterns = _DEFAULT_PII_PATTERNS if redact is True else [re.compile(p) for p in redact]

    def _scrub(_key: str, value: str) -> str:
        out = value
        for pat in patterns:
            out = pat.sub(_REDACTED, out)
        return out

    return _scrub


def _scrubbed_mapping(
    attrs: Any, redactor: Callable[[str, str], str]
) -> dict[str, Any] | None:
    """Return a NEW attribute dict with `redactor` applied to every string (or string-sequence)
    value, or None when nothing changed.

    Deliberately not in place: from OTel SDK 1.29 on, a finished span's attributes are a
    `BoundedAttributes` mapping whose `__setitem__` raises `TypeError`. Mutating it therefore
    scrubbed nothing, and because the failure was swallowed the redaction silently no-opped —
    PII reached the exporter while `init(redact=...)` reported success. The caller swaps the
    whole mapping instead; see `_replace_attributes`."""
    if not attrs:
        return None
    out: dict[str, Any] = {}
    changed = False
    for k, v in attrs.items():
        nv: Any = v
        try:
            if isinstance(v, str):
                nv = redactor(k, v)
            elif isinstance(v, (list, tuple)) and v and all(isinstance(x, str) for x in v):
                scrubbed = [redactor(k, x) for x in v]
                if scrubbed != list(v):
                    nv = scrubbed
        except Exception:  # noqa: BLE001 — redaction must never break the export path
            nv = v
        if nv != v:
            changed = True
        out[k] = nv
    return out if changed else None


def _replace_attributes(target: Any, attrs: dict[str, Any]) -> bool:
    """Swap the attribute mapping on a ReadableSpan or Event. Returns False when the object does
    not allow it — the one case where redaction cannot be applied, which is logged rather than
    passed over in silence."""
    for setter in (lambda: object.__setattr__(target, "_attributes", attrs),
                   lambda: setattr(target, "_attributes", attrs)):
        try:
            setter()
            return True
        except Exception:  # noqa: BLE001 — try the next strategy
            continue
    return False


class _RedactingSpanExporter:
    """Decorates the OTLP exporter: scrubs span + event attributes through `redactor` just before
    handing spans to the wrapped exporter. Implements the SpanExporter duck-type (export/shutdown/
    force_flush) so BatchSpanProcessor treats it as the exporter."""

    def __init__(self, inner: Any, redactor: Callable[[str, str], str]) -> None:
        self._inner = inner
        self._redactor = redactor

    def export(self, spans: Any) -> Any:
        for span in spans:
            self._scrub(span, getattr(span, "_attributes", None))
            for ev in getattr(span, "events", None) or ():
                self._scrub(ev, getattr(ev, "attributes", None))
        return self._inner.export(spans)

    def _scrub(self, target: Any, attrs: Any) -> None:
        scrubbed = _scrubbed_mapping(attrs, self._redactor)
        if scrubbed is None:  # nothing matched — leave the original mapping alone
            return
        if not _replace_attributes(target, scrubbed):
            log.warning(
                "tracely: redaction could not be applied to %s — sensitive values may leave "
                "this process unredacted", type(target).__name__,
            )

    def shutdown(self) -> Any:
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


class _ScopeFilteredBatchProcessor(BatchSpanProcessor):
    """Export only spans whose instrumentation scope starts with one of `scopes` (plus the SDK's
    own). For `init(tracer_provider=...)`: a host app's provider also carries its HTTP/DB/queue
    instrumentation, which is noise in an agent trace — and volume you pay for."""

    def __init__(self, exporter: Any, scopes: tuple[str, ...]):
        super().__init__(exporter)
        self._scopes = (*scopes, "tracely-sdk")

    def on_end(self, span: Any) -> None:
        scope = getattr(getattr(span, "instrumentation_scope", None), "name", "") or ""
        if scope.startswith(self._scopes):
            super().on_end(span)


def init(
    endpoint: str = "http://localhost:8000",
    api_key: str = "tracely_dev_key",
    service_name: str = _DEFAULT_SERVICE_NAME,
    agent: str = "",
    tenant: str = "",
    env: str = "prod",
    instrument: str | list[str] | bool = "auto",
    redact: bool | list[str] | Callable[[str, str], str] | None = None,
    tracer_provider: TracerProvider | None = None,
    export_scopes: tuple[str, ...] | list[str] | None = None,
) -> otel_trace.Tracer:
    """One-call setup (R1). Configures the OTel provider + OTLP exporter pointing at Tracely,
    registers the context-stamping processor, and activates the matching auto-instrumentors so your
    existing OpenAI/Anthropic/… code is traced with zero span code.

    `agent` — the agent name every trace is filed under (the dimension Tracely groups, clusters
    and gates on), defaulting to `service_name`. Tracely never guesses it from framework
    attributes — a harness names every sub-agent it spins up, and reading that registered dozens of
    agents nobody chose — so pass it here only when the agent's name differs from the service's;
    `tracely.trace(agent=…)` still wins per run. Name neither and every trace lands under a single
    `default` agent.

    `tenant` — for a process that serves exactly one customer / workspace / bot: the same as passing
    `tracely.trace(tenant=…)` on every run. See `trace()` for what a tenant is.

    `instrument`:
      - "auto" (default) — activate instrumentors for whatever provider SDKs are importable.
      - ["openai", "anthropic", "litellm", …] — activate exactly these.
      - False — set up export only; no auto-instrumentation (use the manual API / @observe).

    `redact` — scrub sensitive content from span/event attributes BEFORE they leave the process
    (applied at export, so it covers both manual `set_io`/metadata AND zero-touch auto-instrumentor
    prompts/completions/args). For regulated data this is the adoption gate; off by default.
      - None / False (default) — no redaction; payloads ship verbatim.
      - True — apply the built-in PII patterns (email, phone, SSN, credit-card-shaped digit runs).
      - ["regex", …] — replace every match of these patterns with `[REDACTED]`.
      - callable `(attr_key, value) -> value` — full control; return the scrubbed string.
    Set it on the FIRST `init()` call (the exporter is built once).

    `tracer_provider` — attach to a provider the host app already owns instead of building one.
    Required when something else set the global provider first (a web framework's telemetry, an
    OTel-instrumented service): OTel ignores a second `set_tracer_provider`, so the default path
    would silently export nothing. Pair it with `export_scopes` to ship only your agent's spans.

    `export_scopes` — instrumentation-scope name prefixes to export, e.g.
    `("my_app.agents", "openinference.instrumentation.langchain")`. Only meaningful with
    `tracer_provider`; the SDK's own scope is always included.

    Call once at startup; idempotent (provider built once; instrumentor activation de-duped, R7).
    Streaming token usage requires `stream_options={"include_usage": True}` on OpenAI calls (R3)."""
    global _tracer, _provider, _env, _agent, _tenant, _initialized
    _env = env
    _agent = agent or (service_name if service_name != _DEFAULT_SERVICE_NAME else "") or _agent
    _tenant = tenant or _tenant
    # The read path (`export_conversations`) reuses this, so a process that traces can also export
    # without restating the endpoint. OUTSIDE the build-once guard below: the exporter is built once,
    # but an app that already called init() must still be able to point a later, explicit
    # init(endpoint=…, api_key=…) at a workspace and have the export follow it — silently ignoring
    # that call is worse than reads and writes disagreeing.
    remember_connection(endpoint, api_key)
    if not (_initialized and _provider is not None):
        provider = tracer_provider
        if provider is None:
            provider = TracerProvider(
                resource=Resource.create(
                    {"service.name": service_name, "telemetry.sdk.language": "python"}
                )
            )
        provider.add_span_processor(TracelyContextSpanProcessor())  # stamps tracely.* on every span
        exporter: Any = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        redactor = _build_redactor(redact)
        if redactor is not None:
            # Wrap the OTLP exporter so EVERY span (manual + auto-instrumentor) is scrubbed on the
            # way out — the one chokepoint all spans pass through.
            exporter = _RedactingSpanExporter(exporter, redactor)
        provider.add_span_processor(
            _ScopeFilteredBatchProcessor(exporter, tuple(export_scopes))
            if export_scopes
            else BatchSpanProcessor(exporter)
        )
        if tracer_provider is None:  # someone else's provider stays theirs — never steal the global
            otel_trace.set_tracer_provider(provider)
        _provider = provider
        _tracer = provider.get_tracer("tracely-sdk")
        _initialized = True
    # OpenInference replaces any base64 image data URL longer than 32,000 chars with the literal
    # "__REDACTED__" before the span leaves the process — that cap is below a single phone photo, so
    # every real vision input arrived unviewable. Raise it (still bounded; the exporter has to carry
    # it) unless the host explicitly set the env var.
    os.environ.setdefault("OPENINFERENCE_BASE64_IMAGE_MAX_LENGTH", "4000000")
    _activate_instrumentors(instrument)  # idempotent; re-runnable to add providers later
    return _tracer  # type: ignore[return-value]


def _t() -> otel_trace.Tracer:
    if _tracer is None:
        init()
    assert _tracer is not None
    return _tracer


# ── auto-instrumentation (L1) ────────────────────────────────────────────────
# Adopt the OTel ecosystem (D1): per provider, try OpenInference (Arize) then OpenLLMetry
# (Traceloop) — the first importable wins, so a provider is instrumented by exactly ONE path (no
# double spans, R7). Backend ingests both gen_ai.* and llm.* independently (D3), so either works.

# canonical provider/harness -> ordered (module, class) instrumentor candidates; first importable
# activates. OpenInference (Arize) is primary; OpenLLMetry (Traceloop) secondary where it ships one.
_INSTRUMENTORS: dict[str, list[tuple[str, str]]] = {
    # frontier providers
    "openai": [
        ("openinference.instrumentation.openai", "OpenAIInstrumentor"),
        ("opentelemetry.instrumentation.openai_v2", "OpenAIInstrumentor"),
        ("opentelemetry.instrumentation.openai", "OpenAIInstrumentor"),
    ],
    "anthropic": [
        ("openinference.instrumentation.anthropic", "AnthropicInstrumentor"),
        ("opentelemetry.instrumentation.anthropic", "AnthropicInstrumentor"),
    ],
    "google": [
        ("openinference.instrumentation.google_genai", "GoogleGenAIInstrumentor"),
        ("opentelemetry.instrumentation.google_generativeai", "GoogleGenerativeAiInstrumentor"),
    ],
    "mistral": [("openinference.instrumentation.mistralai", "MistralAIInstrumentor")],
    "bedrock": [("openinference.instrumentation.bedrock", "BedrockInstrumentor")],
    "groq": [("openinference.instrumentation.groq", "GroqInstrumentor")],
    # harnesses (orchestration frameworks)
    "langchain": [  # also covers LangGraph (built on LangChain's callback system)
        ("openinference.instrumentation.langchain", "LangChainInstrumentor"),
        ("opentelemetry.instrumentation.langchain", "LangchainInstrumentor"),
    ],
    "llama-index": [("openinference.instrumentation.llama_index", "LlamaIndexInstrumentor")],
    "crewai": [("openinference.instrumentation.crewai", "CrewAIInstrumentor")],
    # first-party agent SDKs (each emits AGENT/TOOL/LLM spans via its OpenInference instrumentor)
    "openai-agents": [("openinference.instrumentation.openai_agents", "OpenAIAgentsInstrumentor")],
    "google-adk": [("openinference.instrumentation.google_adk", "GoogleADKInstrumentor")],
    "claude-agent-sdk": [
        ("openinference.instrumentation.claude_agent_sdk", "ClaudeAgentSDKInstrumentor")
    ],
}
# provider/harness keys that wrap an LLM provider directly (vs. harnesses that route through them) —
# used by the LangChain de-dup guard to know what to suppress under "auto".
_PROVIDER_KEYS = frozenset({"openai", "anthropic", "google", "mistral", "bedrock", "groq"})
# aliases normalized to a canonical key
_ALIASES = {
    "gemini": "google",
    "google-genai": "google",
    "googleai": "google",
    "genai": "google",
    "mistralai": "mistral",
    "llama_index": "llama-index",
    "llamaindex": "llama-index",
    "aws": "bedrock",
    "bedrock-runtime": "bedrock",
    "openai_agents": "openai-agents",
    "openai-agents-sdk": "openai-agents",
    "agents": "openai-agents",
    "adk": "google-adk",
    "google_adk": "google-adk",
    "claude-agent": "claude-agent-sdk",
    "claude_agent_sdk": "claude-agent-sdk",
}
# SDK import name used to detect a provider for instrument="auto". Only providers whose SDK presence
# strongly implies intent to trace them (litellm/bedrock are opt-in: a router / boto3 is too common).
_PROVIDER_SDK: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google.genai",
    "mistral": "mistralai",
    "langchain": "langchain_core",
}
_AUTO_PROVIDERS = ("openai", "anthropic", "google", "mistral", "langchain")
_instrumented: set[str] = set()


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _import_attr(module: str, attr: str) -> Any:
    import importlib

    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError):
        return None


def _activate_litellm() -> bool:
    """Route LiteLLM's 100+ providers through OTel via its `otel` callback (R12). LiteLLM is
    opt-in (not part of "auto") because instrumenting both LiteLLM and a provider SDK double-traces
    calls LiteLLM makes to that provider — disable the overlap with OTEL_PYTHON_DISABLED_INSTRUMENTATIONS (R7)."""
    import importlib

    try:
        litellm = importlib.import_module("litellm")
    except ImportError:
        log.warning('tracely: instrument=["litellm"] requested but litellm is not installed')
        return False
    cbs = list(getattr(litellm, "callbacks", None) or [])
    if "otel" not in cbs:
        cbs.append("otel")
    litellm.callbacks = cbs
    _instrumented.add("litellm")
    log.info("tracely: enabled litellm otel callback")
    return True


def _has_instrumentor(name: str) -> bool:
    """Is an instrumentor package for `name` importable (not just the provider SDK)?"""
    return any(_module_available(mod) for mod, _ in _INSTRUMENTORS.get(name, []))


def _activate_one(name: str) -> bool:
    name = _ALIASES.get(name, name)  # gemini -> google, etc.
    if name in _instrumented:
        return True  # idempotent — one path per provider (R7)
    if name == "litellm":
        return _activate_litellm()
    for module, cls in _INSTRUMENTORS.get(name, []):
        instr_cls = _import_attr(module, cls)
        if instr_cls is None:
            continue
        try:
            instr_cls().instrument(tracer_provider=_provider)
            _instrumented.add(name)
            log.info("tracely: instrumented %s via %s", name, module)
            return True
        except Exception as e:  # a broken/older instrumentor shouldn't crash startup
            log.warning("tracely: failed to instrument %s via %s: %s", name, module, e)
    if name not in _INSTRUMENTORS:
        # An unknown target was a silent no-op — `instrument=["openrouter"]` traced nothing and
        # never said so. (OpenRouter rides through the langchain instrumentor.)
        log.warning(
            "tracely: unknown instrument target %r — known targets: %s",
            name,
            ", ".join(sorted([*_INSTRUMENTORS, "litellm"])),
        )
        return False
    if _module_available(_PROVIDER_SDK.get(name, name)):  # SDK present but instrumentor missing
        log.warning(
            'tracely: %s is installed but no instrumentor found — pip install "tracely-ai[%s]"',
            name,
            name,
        )
    return False


def _resolve_targets(instrument: str | list[str] | bool) -> list[str]:
    """Which providers to activate. "auto"/True → installed SDKs (with the LangChain de-dup guard);
    a list → exactly those (honored as-is — the override); False/None → none."""
    if instrument in (False, None):
        return []
    if instrument in ("auto", True):
        targets = [p for p in _AUTO_PROVIDERS if _module_available(_PROVIDER_SDK[p])]
        # De-dup guard (R7): LangChain routes through the provider SDKs, so running the LangChain
        # instrumentor AND a provider instrumentor double-traces LangChain→provider calls (sibling
        # spans). In "auto", when the LangChain instrumentor is installed, it owns LLM spans — skip
        # the provider instrumentors. Override by passing an explicit list (honored as-is).
        if "langchain" in targets and _has_instrumentor("langchain"):
            dropped = [p for p in targets if p in _PROVIDER_KEYS]
            if dropped:
                log.warning(
                    "tracely: LangChain instrumentation active — skipping %s auto-instrumentation to "
                    "avoid duplicate spans. Pass instrument=%r to force both.",
                    dropped,
                    ["langchain", *dropped],
                )
                targets = [p for p in targets if p not in dropped]
        return targets
    if isinstance(instrument, str):
        return [instrument]
    return [str(x) for x in instrument]


def _activate_instrumentors(instrument: str | list[str] | bool) -> None:
    for name in _resolve_targets(instrument):
        _activate_one(name.lower())


# ── run context (L3) ─────────────────────────────────────────────────────────
# `tracely.trace(...)` sets the run-level tracely.* hints once; the span processor flows them onto
# every child span (auto or manual), replacing today's per-span agent=/conversation= plumbing (R9).


def _remote_context(traceparent: str) -> Any:
    """A W3C `traceparent` header → the OTel context to run under, or None if it carries no usable
    span.

    Explicitly `TraceContextTextMapPropagator` rather than the global `propagate.extract`: the
    caller handed us a `traceparent` by name, and resolving it through whatever `OTEL_PROPAGATORS`
    happens to be set to would make "did my span join the parent trace?" depend on unrelated
    configuration.

    A malformed header returns None *and logs*. Silently starting a new root is what this argument
    exists to prevent — a caller who passed a header and got no correlation would be looking at the
    same disconnected trace with nothing to explain it.
    """
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    if not otel_trace.get_current_span(ctx).get_span_context().is_valid:
        log.warning(
            "tracely: ignoring unusable traceparent %r — this run starts its own trace and will "
            "not join the caller's", traceparent[:64],
        )
        return None
    return ctx


class _Trace:
    """The object returned by `tracely.trace(...)`. Usable three ways — as a context manager
    (`with tracely.trace(...):`), a sync decorator, or an async decorator. Each entry merges its
    fields over the enclosing run context (so nested traces inherit + override), and resets on
    exit. It sets context only — it does not open a span (an `@observe`/`agent()` span, or the
    auto-instrumentor's own span, becomes the root).

    With `traceparent` it also attaches the caller's trace context for the block, so that root span
    becomes a CHILD of the caller's span instead of starting a trace of its own."""

    __slots__ = ("_fields", "_token", "_traceparent", "_ctx_token")

    def __init__(self, fields: dict[str, Any], traceparent: str | None = None):
        self._fields = {k: v for k, v in fields.items() if v not in (None, {})}
        self._token: Any = None
        self._traceparent = traceparent
        self._ctx_token: Any = None

    def __enter__(self) -> dict[str, Any]:
        parent = _run_ctx.get() or {}
        merged = {**parent, **self._fields}
        merged["metadata"] = {**parent.get("metadata", {}), **self._fields.get("metadata", {})}
        self._token = _run_ctx.set(merged)
        if self._traceparent:
            remote = _remote_context(self._traceparent)
            if remote is not None:
                from opentelemetry import context as otel_context

                self._ctx_token = otel_context.attach(remote)
        return merged

    def __exit__(self, *exc: Any) -> bool:
        if self._ctx_token is not None:
            from opentelemetry import context as otel_context

            otel_context.detach(self._ctx_token)
            self._ctx_token = None
        if self._token is not None:
            _run_ctx.reset(self._token)
            self._token = None
        return False

    def __call__(self, fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*a: Any, **k: Any) -> Any:
                with _Trace(self._fields, self._traceparent):
                    return await fn(*a, **k)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*a: Any, **k: Any) -> Any:
            with _Trace(self._fields, self._traceparent):
                return fn(*a, **k)

        return wrapper


def trace(
    agent: str | None = None,
    *,
    tenant: str | None = None,
    conversation: str | None = None,
    turn: int | None = None,
    turn_id: str | None = None,
    user: str | None = None,
    trace_name: str | None = None,
    env: str | None = None,
    agents: list[dict] | None = None,
    traceparent: str | None = None,
    **metadata: Any,
) -> _Trace:
    """Open a run context: set `agent`/`conversation`/`turn`/`turn_id`/`user`/`trace_name`/`env`
    (+ arbitrary `metadata`) once, and every span started inside — including zero-touch provider
    spans from the auto-instrumentors — inherits them via the context processor (R9/R4). Use as a
    context manager or a (sync/async) decorator. Nested `trace()`s merge over the enclosing one.

    `traceparent` joins the caller's trace: pass the incoming request's W3C header and every span in
    the block nests under the caller's span instead of starting a new trace.

        @app.post("/chat")
        def chat(req: ChatRequest, request: Request):
            with tracely.trace(agent="support",
                               conversation=req.session_id,
                               traceparent=request.headers.get("traceparent")):
                return run_agent(req.message)

    This is what makes a Tracely-driven conversation (`tracely simulate`, the scenario gate) show
    your real trajectory: Tracely mints the trace id, sends it as `traceparent`, and POSTs its
    conversation id in the body — so honour both and your tool calls land inside the turn instead of
    on a disconnected trace of their own. Without it Tracely sees only the request and the reply, and
    every step/tool-level evaluator has nothing to grade. Also correct for any ordinary upstream
    caller that already traces. A header that carries no usable span is ignored (logged), never
    fatal; if your web framework already has OTel server instrumentation, the context is ambient and
    you can leave this unset.

    `tenant` is which customer / workspace / bot this conversation belongs to, when one codebase
    serves many. Tracely registers each tenant as its own Agent — the thing with an endpoint,
    scenarios, a CI gate, failure clusters and regression cases — so a per-customer deployment gets
    per-customer gating without touching `agent`, which stays the label on each span ("supervisor",
    "billing") in the trace's Agent column. Filter the traces list by it; `tracely gate <tenant>`.

        with tracely.trace(agent="supervisor", tenant=customer_id, conversation=thread_id):
            run_agent(message)

    `turn` is the ordinal (0, 1, 2…); `turn_id` is your own id for one exchange, and unlike the
    `turn()` span helper it lands on EVERY span of the turn, which is what the `turn_id` column
    groups on.

    `agents` declares the conversation's agent catalog — a list of
    `{name, description, tools: {tool_name: {name, description, parameters}}}` — surfaced in the
    Conversation Agents panel and usable in evaluation (`@LIST_AGENT`). Set it once on the first
    turn (or every turn; the backend keeps the latest per conversation).

    Each agent is free-form JSON: only `name`/`description`/`tools` are interpreted, and every
    other key is stored and returned verbatim. Declare whatever the trace can't show you —
    `system_prompt`, `model`, `guardrails`, or a whole `config` blob:

        tracely.trace(conversation="c1", agents=[{
            "name": "router",
            "description": "picks the specialist",
            "system_prompt": ROUTER_PROMPT,
            "model": {"name": "claude-opus-4", "temperature": 0.2},
            "guardrails": [{"name": "pii_filter", "on": "output", "action": "redact"}],
            "tools": {"search": {"description": "web search", "parameters": {...}}},
        }])

    Guardrails in particular are invisible to tracing — one that passes emits no span — so
    declaring them is the only way they show up. Non-Python callers can POST the same list to
    `/api/sessions/{conversation}/config`."""
    return _Trace(
        {
            "agent": agent,
            "tenant": tenant,
            "conversation": conversation,
            "turn": turn,
            "turn_id": turn_id,
            "user": user,
            "trace_name": trace_name,
            "env": env,
            "agents": agents,
            "metadata": {k: v for k, v in metadata.items()},
        },
        # Not a run-context field: it addresses the caller's span, it is not an attribute of ours.
        traceparent=traceparent,
    )


# ── @observe (L2) ────────────────────────────────────────────────────────────


def _capture_args(span: Span, func: Callable, a: tuple, k: dict) -> dict:
    """Bind call args to parameter names → `tracely.input` (best effort; drops self/cls). Returns the
    bound dict so the caller can reuse it (e.g. fixture arg-matching in hermetic replay)."""
    try:
        bound = inspect.signature(func).bind(*a, **k)
        bound.apply_defaults()
        args = dict(bound.arguments)
        args.pop("self", None)
        args.pop("cls", None)
    except (TypeError, ValueError):
        args = {"args": list(a), **({"kwargs": k} if k else {})}
    if args:
        set_io(span, input=args)
    return args


def _replay_observed_tool(span: Span, name: str, args: Any) -> tuple[bool, Any]:
    """The hermetic-replay bridge for `@observe(as_type="tool")` — the decorator twin of `call_tool`.

    In a `with fixtures(bundle):` block, serve the next recorded entry for this tool instead of
    running it: stamp `tracely.replay.fixture`, set the recorded output, and (if the production call
    errored) mark the span ERROR + raise `ToolError` so the agent's own error handling runs. Returns
    `(handled, output)` — `handled=False` means "no fixture active / none recorded for this tool",
    so the caller runs the real function (this is a strict no-op in production, where `_fixtures` is
    unset). This is what lets an auto-instrumented agent whose tools are merely `@observe`-decorated
    replay deterministically in CI, with no `call_tool` rewrite."""
    entry = _pop_fixture("tools", name, args)
    if entry is None:
        return False, None
    span.set_attribute("tracely.replay.fixture", True)
    if entry.get("output") is not None:
        set_io(span, output=entry.get("output"))
    if entry.get("error"):
        error(span, str(entry["error"]))
        raise ToolError(str(entry["error"]))
    return True, entry.get("output")


def observe(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    as_type: str = "span",
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable:
    """Wrap any sync/async function as a span (R8): args→input, return→output, latency, and
    exceptions (→ level=ERROR) captured automatically; auto-nests via OTel context — no manual
    parent wiring. `as_type` (span | generation | agent | tool | chain | retriever | thinking | embedding |
    guardrail | delegate | skill | …) becomes `tracely.observation.type`. Usable as `@observe` or
    `@observe(...)`."""

    def decorate(func: Callable) -> Callable:
        span_name = name or getattr(func, "__name__", "observed")
        otype = str(as_type).upper()

        @functools.wraps(func)
        def sync_wrapper(*a: Any, **k: Any) -> Any:
            with _t().start_as_current_span(span_name) as span:
                span.set_attribute("tracely.observation.type", otype)
                bound = _capture_args(span, func, a, k) if capture_input else None
                if otype == "TOOL":  # hermetic-replay bridge: serve a fixture instead of running
                    handled, replayed = _replay_observed_tool(span, span_name, bound)
                    if handled:
                        return replayed
                try:
                    out = func(*a, **k)
                except Exception as e:
                    error(span, str(e))
                    raise
                if capture_output and out is not None:
                    set_io(span, output=out)
                return out

        @functools.wraps(func)
        async def async_wrapper(*a: Any, **k: Any) -> Any:
            with _t().start_as_current_span(span_name) as span:
                span.set_attribute("tracely.observation.type", otype)
                bound = _capture_args(span, func, a, k) if capture_input else None
                if otype == "TOOL":  # hermetic-replay bridge: serve a fixture instead of running
                    handled, replayed = _replay_observed_tool(span, span_name, bound)
                    if handled:
                        return replayed
                try:
                    out = await func(*a, **k)
                except Exception as e:
                    error(span, str(e))
                    raise
                if capture_output and out is not None:
                    set_io(span, output=out)
                return out

        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

    return decorate(fn) if callable(fn) else decorate


# ── threads (R10) ────────────────────────────────────────────────────────────
# Auto-nesting is contextvar-based and in-process: a raw threading.Thread starts with a fresh,
# detached context, so spans it creates would NOT nest under the current span or see trace().


class _ContextThread(threading.Thread):
    def __init__(self, ctx: Any, fn: Callable, args: tuple, kwargs: dict):
        super().__init__()
        self._ctx, self._fn, self._args, self._kwargs = ctx, fn, args, kwargs
        self.result: Any = None
        self.exc: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._ctx.run(self._fn, *self._args, **self._kwargs)
        except BaseException as e:  # surfaced to the caller via `.exc` after join()
            self.exc = e


def run_in_thread(fn: Callable, *args: Any, **kwargs: Any) -> _ContextThread:
    """Run `fn` in a new thread that inherits the current trace context, so spans it creates nest
    under the active span / `tracely.trace(...)` (R10). Returns the started Thread — `join()` it,
    then read `.result` (or `.exc`). For thread pools, wrap the callable with
    `contextvars.copy_context().run` per task the same way."""
    th = _ContextThread(copy_context(), fn, args, kwargs)
    th.start()
    return th


@contextmanager
def agent(
    slug: str,
    *,
    version: str | None = None,
    run_id: str | None = None,
    role: str | None = None,
    conversation: str | None = None,
    turn: int | None = None,
    user: str | None = None,
    trace_name: str | None = None,
    handoff_from: str | None = None,
    edge: str = "delegate",
) -> Iterator[Span]:
    """An agent run. `conversation` groups runs into a thread (a multi-turn session); `version`
    is auto-registered for the regression gate. On the run's root, set `user` (end-user id) and
    `trace_name` (a human label). For a sub-agent invoked by another, pass `handoff_from` (the
    caller's slug) to record the delegation edge (caller → this agent, `edge` = relationship)."""
    with _t().start_as_current_span(slug) as span:
        span.set_attribute("tracely.agent.id", slug)
        span.set_attribute("tracely.observation.type", "AGENT")
        # tracely.env is stamped by TracelyContextSpanProcessor (run-ctx value, else init() default)
        if version:
            span.set_attribute("tracely.agent.version", version)
        if run_id:
            span.set_attribute("tracely.agent.run_id", run_id)
        if role:
            span.set_attribute("tracely.agent.role", role)
        if conversation:  # groups runs into a thread (session)
            span.set_attribute("tracely.conversation.id", conversation)
            span.set_attribute("session.id", conversation)
        if turn is not None:
            span.set_attribute("tracely.turn.index", int(turn))
        if user:
            span.set_attribute("tracely.user.id", user)
        if trace_name:
            span.set_attribute("tracely.trace.name", trace_name)
        if handoff_from:  # this agent was delegated to by `handoff_from` (a handoff edge)
            span.set_attribute("tracely.handoff.caller_agent_id", handoff_from)
            span.set_attribute("tracely.handoff.callee_agent_id", slug)
            span.set_attribute("tracely.edge.type", edge)
        yield span


@contextmanager
def turn(turn_id: str, *, index: int | None = None) -> Iterator[Span]:
    with _t().start_as_current_span(f"turn:{turn_id}") as span:
        span.set_attribute("tracely.turn.id", turn_id)
        if index is not None:
            span.set_attribute("tracely.turn.index", index)
        yield span


@contextmanager
def step(name: str, *, step_id: str | None = None) -> Iterator[Span]:
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.step.name", name)
        if step_id:
            span.set_attribute("tracely.step.id", step_id)
        yield span


@contextmanager
def llm(
    model: str,
    *,
    agent: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    frequency_penalty: float | None = None,
    presence_penalty: float | None = None,
    seed: int | None = None,
    metadata: dict[str, Any] | None = None,
    tool_calls: list[str] | None = None,
) -> Iterator[Span]:
    """An LLM generation. Pass the sampling parameters (temperature/top_p/max_tokens/…) — they're
    recorded as standard `gen_ai.request.*` attributes and surfaced in the generation's Metadata.
    `metadata` attaches arbitrary key/values (e.g. prompt version, tenant). `tool_calls` records the
    tools the model REQUESTED this turn (even if a tool never runs — the silent-failure signal)."""
    with _t().start_as_current_span(model) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        for key, val in (
            ("gen_ai.request.temperature", temperature),
            ("gen_ai.request.top_p", top_p),
            ("gen_ai.request.max_tokens", max_tokens),
            ("gen_ai.request.frequency_penalty", frequency_penalty),
            ("gen_ai.request.presence_penalty", presence_penalty),
            ("gen_ai.request.seed", seed),
        ):
            if val is not None:
                span.set_attribute(key, val)
        if tool_calls:
            span.set_attribute("tracely.tool_calls", list(tool_calls))
        if metadata:
            set_metadata(span, **metadata)
        yield span


@contextmanager
def tool(name: str, *, agent: str | None = None) -> Iterator[Span]:
    with _t().start_as_current_span(name) as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        yield span


@contextmanager
def thinking(
    name: str = "thinking", *, agent: str | None = None, model: str | None = None
) -> Iterator[Span]:
    """A reasoning step. First-class observation type THINKING — the model's chain-of-thought,
    emitted as its own span so it shows up distinctly from the GENERATION that follows. Put the
    reasoning text in `set_io(span, output=...)` and reasoning tokens in `set_usage(..., thinking_tokens=)`.
    Pass `model` to record which model produced the reasoning (shown in the Model column)."""
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.observation.type", "THINKING")
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        if model:
            span.set_attribute("gen_ai.request.model", model)
        yield span


@contextmanager
def retriever(name: str = "retrieve", *, agent: str | None = None) -> Iterator[Span]:
    """A retrieval step (vector / keyword / web search). Put the query in `set_io(input=...)` and
    the hits in `set_io(output=...)`; tag the store/index with `set_metadata`."""
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.observation.type", "RETRIEVER")
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        yield span


@contextmanager
def embedding(model: str, *, agent: str | None = None) -> Iterator[Span]:
    """An embedding call. Record token usage with `set_usage(input_tokens=...)`; the embedded text
    goes in `set_io(input=...)`."""
    with _t().start_as_current_span(model) as span:
        span.set_attribute("tracely.observation.type", "EMBEDDING")
        span.set_attribute("gen_ai.request.model", model)
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        yield span


@contextmanager
def guardrail(name: str = "guardrail", *, agent: str | None = None) -> Iterator[Span]:
    """A safety / policy check. Put the input in `set_io(input=...)` and the verdict in
    `set_io(output={"action": "allow" | "block", ...})`."""
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.observation.type", "GUARDRAIL")
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        yield span


@contextmanager
def chain(name: str, *, agent: str | None = None) -> Iterator[Span]:
    """A grouping span (a named sub-pipeline, e.g. a RAG pipeline). Nest other spans inside it."""
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.observation.type", "CHAIN")
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        yield span


@contextmanager
def delegate(
    to: str, *, agent: str | None = None, task: str | None = None, edge: str = "delegate"
) -> Iterator[Span]:
    """A handover: `agent` (the caller) hands work to `to` (the callee). Type DELEGATE.

    This is the *act* of delegating — open the callee's `agent(...)` inside it, and the span
    brackets everything the callee did for this one job. It records the same handoff edge as
    `agent(handoff_from=...)`, so the multi-agent graph is drawn either way; the difference is
    that a delegate span also carries the routing decision itself — put the reason the caller
    picked this agent in `set_io(span, input=...)` and what came back in `output=`, and a
    step-level judge can grade the routing separately from the work.
    """
    with _t().start_as_current_span(f"delegate:{to}") as span:
        span.set_attribute("tracely.observation.type", "DELEGATE")
        span.set_attribute("tracely.handoff.callee_agent_id", to)
        span.set_attribute("tracely.edge.type", edge)
        if agent:
            span.set_attribute("tracely.agent.id", agent)
            span.set_attribute("tracely.handoff.caller_agent_id", agent)
        if task:
            span.set_attribute("tracely.metadata.task", task)
        yield span


@contextmanager
def skill(
    name: str, *, agent: str | None = None, version: str | None = None
) -> Iterator[Span]:
    """A named skill / capability / playbook the agent invoked. Type SKILL.

    A skill is bigger than a tool and smaller than an agent: a named procedure the agent chose to
    run ("refund-flow", "escalate-to-human", a loaded agent-skill file), usually with its own
    tool calls and generations nested inside. Naming it makes "which skill did this?" a filter and
    a per-skill failure cluster instead of a shape you have to infer from the span tree.
    `version` pins which revision of the skill ran — the thing you actually changed when a
    regression appears.
    """
    with _t().start_as_current_span(name) as span:
        span.set_attribute("tracely.observation.type", "SKILL")
        span.set_attribute("tracely.step.name", name)
        if agent:
            span.set_attribute("tracely.agent.id", agent)
        if version:
            span.set_attribute("tracely.metadata.skill_version", version)
        yield span


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


def set_io(span: Span, *, input: Any = None, output: Any = None) -> None:
    if input is not None:
        span.set_attribute("tracely.input", _as_str(input))
    if output is not None:
        span.set_attribute("tracely.output", _as_str(output))


def set_metadata(span: Span, **kv: Any) -> None:
    """Attach arbitrary metadata to a span as `tracely.metadata.<key>` attributes — surfaced in the
    UI's Metadata column / span panel (and searchable). Non-scalar values are JSON-encoded."""
    for k, v in kv.items():
        if v is None:
            continue
        span.set_attribute(
            f"tracely.metadata.{k}",
            v if isinstance(v, (str, int, float, bool)) else json.dumps(v, default=str),
        )


def set_state(delta: dict, span: Span | None = None, *, max_bytes: int = 4096) -> None:
    """Record a shared-state delta — the LangGraph `State` channels a step wrote, a scratchpad the
    graph threads between nodes, a conversation-scoped store. One `tracely.state.<key>` attribute
    per key (not one blob), so ClickHouse can read a single channel without parsing everything.

    The SCOPE is whichever span you attach it to, because the span already carries the level:
    the `tracely.trace(...)` root span → conversation-wide · a `turn()` span → this message ·
    a `step()` span → this step. Defaults to the current span, which is usually what you want.

        with tracely.step("planner") as s:
            plan = make_plan()
            tracely.set_state({"plan": plan, "retries": 0})

    Record the DELTA (what this step changed), not a full snapshot: the State panel folds deltas
    back into the whole object, and re-stamping the entire state on every span bloats the trace
    for no gain. Values over `max_bytes` are truncated per key, so one fat channel (a message
    history, a blob of retrieved docs) can't crowd out the small ones that matter.

    LangGraph users on the auto instrumentor need none of this — a node's return value is already
    captured as the span's output and read as a delta. Use this for state your harness manages
    itself, or to promote something the instrumentor can't see.

    The implicit span is the *OTel* current span, which exists inside `step()`/`tool()`/`llm()`/…
    but NOT inside an auto-instrumented framework callback: `tracely.trace()` is a context marker
    rather than an active span, and LangGraph runs node functions with no span in context. So a
    bare `set_state()` inside a LangGraph node has nothing to attach to — it warns rather than
    dropping the write silently. Pass an explicit `span` there, or rely on the automatic capture.
    """
    if not delta:
        return
    target = span if span is not None else otel_trace.get_current_span()
    if target is None or not target.is_recording():
        log.warning(
            "tracely.set_state(%s): no recording span to attach to — pass span=... explicitly "
            "(inside an auto-instrumented framework callback there is no current span)",
            ",".join(map(str, delta)),
        )
        return
    for k, v in delta.items():
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        if len(s) > max_bytes:
            s = s[:max_bytes] + "…[truncated]"
        target.set_attribute(f"tracely.state.{k}", s)


def set_agents(span: Span, agents: list[dict]) -> None:
    """Declare the conversation's agent catalog on a span: a list of
    `{name, description, tools: {tool_name: {name, description, parameters}}}`, plus any extra
    keys you want kept verbatim (`system_prompt`, `model`, `guardrails`, `config`, …). Surfaced in
    the Conversation Agents panel and usable in evaluation (`@LIST_AGENT`). Prefer
    `tracely.trace(..., agents=[...])`, which flows it onto every span; use this to set it on one
    specific (e.g. the root) span."""
    if agents:
        span.set_attribute("tracely.agents", json.dumps(agents, default=str))


def set_usage(
    span: Span,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    thinking_tokens: int | None = None,
    cached_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> None:
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
    if thinking_tokens is not None:
        span.set_attribute("gen_ai.usage.reasoning_tokens", int(thinking_tokens))
    # Prompt-cache breakdown, informational only: whether it overlaps input_tokens is
    # provider-dependent, so the backend keeps it out of the additive usage map and the UI shows it
    # as its own row. Names match OpenLLMetry's, so auto-instrumented spans land on the same keys.
    if cached_tokens is not None:
        span.set_attribute("gen_ai.usage.cache_read_input_tokens", int(cached_tokens))
    if cache_write_tokens is not None:
        span.set_attribute("gen_ai.usage.cache_creation_input_tokens", int(cache_write_tokens))


def error(span: Span, message: str = "") -> None:
    """Mark a span as failed (level=ERROR + status_message) — the failure-detection signal."""
    span.set_status(Status(StatusCode.ERROR, message))


def flush() -> None:
    # `_provider` first: with init(tracer_provider=...) the SDK's processors live on the host's
    # provider, which may not be the global one.
    provider = _provider or otel_trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


# ── hermetic replay ──────────────────────────────────────────────────────────
# In CI replay we want the agent to see the exact tool/LLM outputs the production trace saw —
# deterministic, offline, no live API keys or cost. `tracely replay` loads each case's recorded
# fixture bundle and activates it here; the agent's call_tool / call_llm then serve from it.


def _normalize_bundle(bundle: dict | None) -> dict:
    """Turn a fixture bundle into consumable FIFO queues keyed by tool/model name.

    Accepts both formats: v2 (`{"version":2, "tools":[{name,args,output,error,...}], "llm":[...]}`,
    ordered so repeated calls and per-call errors replay faithfully) and the legacy v1
    (`{"tools":{name:output}, "llm":{model:output}}`). Returns {"tools": {name:[entry,...]},
    "llm": {model:[entry,...]}} where each entry is {"args","output","error"}.
    """
    store: dict = {"tools": {}, "llm": {}}
    if not bundle:
        return store
    for kind, key_field in (("tools", "name"), ("llm", "model")):
        section = bundle.get(kind)
        if isinstance(section, list):  # v2: ordered list of entries
            for e in section:
                # LLM entries persist the call input under "input" (tool entries use "args");
                # normalize both to "args" so _pop_fixture's arg-matching works for either kind.
                store[kind].setdefault(e.get(key_field), []).append(
                    {
                        "args": e.get("args") if kind == "tools" else e.get("input", e.get("args")),
                        "output": e.get("output"),
                        "error": e.get("error"),
                    }
                )
        elif isinstance(section, dict):  # v1: {name: output}
            for k, v in section.items():
                store[kind].setdefault(k, []).append({"args": None, "output": v, "error": None})
    return store


@contextmanager
def fixtures(bundle: dict | None) -> Iterator[None]:
    """Serve recorded outputs for the duration of this block. Covers all three execution paths:
    the manual seams (call_tool/call_llm), `@observe(as_type="tool")`, and — via provider-client
    patching installed here — the auto-instrument / drop-in path (code that calls the provider SDK
    directly). Entries are consumed in order (so N calls replay the N recorded outputs); pass None
    to leave calls live."""
    normalized = _normalize_bundle(bundle) if bundle else None
    if normalized:
        _install_replay_patches()  # idempotent; only patches importable providers
    token = _fixtures.set(normalized)
    try:
        yield
    finally:
        _warn_unconsumed(_fixtures.get())
        _fixtures.reset(token)


def _unconsumed(store: dict | None) -> list[str]:
    """`kind:key ×n` for every recorded call the replayed run never asked for."""
    left = []
    for kind, by_key in (store or {}).items():
        for key, queue in (by_key or {}).items():
            if queue:
                left.append(f"{kind}:{key} ×{len(queue)}")
    return sorted(left)


def _warn_unconsumed(store: dict | None) -> None:
    """A replay that leaves recorded calls on the table did NOT reproduce the recorded run: the
    agent took a different path, or — the quiet one — it called a provider Tracely cannot patch
    and went to the network instead. Both make the gate's verdict mean less than it appears to,
    so say so out loud rather than letting a live call pass for a hermetic one."""
    left = _unconsumed(store)
    if left:
        log.warning(
            "hermetic replay left %d recorded call(s) unused (%s) — the run took a different "
            "path, or a provider went live because Tracely could not patch it",
            len(left),
            ", ".join(left[:6]) + (" …" if len(left) > 6 else ""),
        )


def _pop_fixture(kind: str, key: str, args: Any = None) -> dict | None:
    """Consume the next recorded entry for a tool/model: an args-match if `args` is given and one
    exists, else the next in recorded order. Returns None if not replaying / nothing recorded."""
    store = _fixtures.get()
    if not store:
        return None
    queue = store.get(kind, {}).get(key)
    if not queue:
        return None
    if args is not None:
        for i, e in enumerate(queue):
            if e.get("args") == args:
                return queue.pop(i)
    return queue.pop(0)


def fixture(kind: str, name: str) -> Any:
    """Peek the next recorded output for a tool/llm by name (non-consuming), or None."""
    store = _fixtures.get()
    if not store:
        return None
    queue = store.get(kind, {}).get(name)
    return queue[0].get("output") if queue else None


def call_tool(
    name: str, fn: Callable[[], Any], *, args: Any = None, agent: str | None = None
) -> Any:
    """Execute a tool inside a TOOL span — but in hermetic replay serve the recorded call and
    never call `fn`. Pass `args` to match a specific recorded call; without it, recorded calls are
    served in order. If the recorded call ERRORED in production, the replayed span is marked ERROR
    and a `ToolError` is raised — so the agent's own error handling runs and the gate sees the same
    failure (faithful error-condition replay). Errors propagate the same way under `--live`."""
    with tool(name, agent=agent) as span:
        if args is not None:
            set_io(span, input=args)
        entry = _pop_fixture("tools", name, args)
        if entry is None:
            out = fn()
            set_io(span, output=out)
            return out
        span.set_attribute("tracely.replay.fixture", True)
        if entry.get("output") is not None:
            set_io(span, output=entry.get("output"))
        if entry.get("error"):
            error(span, str(entry["error"]))
            raise ToolError(str(entry["error"]))
        return entry.get("output")


def call_llm(
    model: str,
    fn: Callable[[], Any],
    *,
    input: Any = None,
    usage: tuple[int, int] | None = None,
    agent: str | None = None,
) -> Any:
    """Execute an LLM call inside a GENERATION span — but in hermetic replay serve the recorded
    completion (in recorded order) and never call `fn`. A recorded error is reproduced on the span
    and raised as a `ToolError`. Pass `usage=(input_tokens, output_tokens)` to report token usage
    (feeds the gate's cost/token soft gate)."""
    with llm(model, agent=agent) as span:
        if input is not None:
            set_io(span, input=input)
        if usage is not None:
            set_usage(span, input_tokens=usage[0], output_tokens=usage[1])
        entry = _pop_fixture("llm", model)
        if entry is None:
            out = fn()
            set_io(span, output=out)
            return out
        span.set_attribute("tracely.replay.fixture", True)
        if entry.get("output") is not None:
            set_io(span, output=entry.get("output"))
        if entry.get("error"):
            error(span, str(entry["error"]))
            raise ToolError(str(entry["error"]))
        return entry.get("output")


# ── auto-instrument / drop-in hermetic replay (provider-client patching) ───────
# call_tool/call_llm/@observe serve fixtures explicitly. But code that uses the auto-instrumentors
# (instrument="auto") or the drop-in client wrappers calls the provider SDK directly, with no Tracely
# seam to short-circuit — so to replay THAT hermetically we patch the provider's create-method at the
# class level. Inside a fixtures() block the patch opens a GENERATION span, serves the recorded
# completion (reconstructed into a provider-shaped object) and never touches the network; outside
# replay it calls straight through (inert). Installed once on the first fixtures() enter; never torn
# down (the patch is a no-op when not replaying). If the auto-instrumentor already wrapped the same
# method, ours is the outer layer and short-circuits before its wrapper runs — so a served call makes
# neither a real request nor a duplicate span.
#
# ponytail: covers OpenAI chat.completions + Anthropic messages (the two dominant paths). Other
# providers slot into _REPLAY_PROVIDERS with a reconstruct fn — add one when a customer replays it.


def _pop_fixture_any(kind: str) -> dict | None:
    """Pop the next recorded entry of `kind` regardless of key — the order-matched fallback for when
    the recorded span name doesn't equal the live model id (auto-instrumentor span names vary)."""
    store = _fixtures.get()
    if not store:
        return None
    for queue in store.get(kind, {}).values():
        if queue:
            return queue.pop(0)
    return None


def _assistant_message(output: Any) -> dict:
    """Resolve a recorded GENERATION output to the assistant message dict, across the shapes the
    record paths + backend normalization produce: a JSON string, a single message dict (optionally
    nested under "message"), or a list of messages (last dict wins). Plain text → {"content": text}."""
    data = output
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return {"content": data}  # plain-text completion
    if isinstance(data, list):
        data = next((m for m in reversed(data) if isinstance(m, dict)), {}) or {}
    if isinstance(data, dict):
        return data["message"] if isinstance(data.get("message"), dict) else data
    return {"content": data}


def _extract_completion(output: Any) -> tuple[Any, list]:
    """(content, tool_calls) for the OpenAI-canonical shape — content is a string, tool_calls the
    `[{id,type,function:{name,arguments}}]` list the backend reassembles every provider into."""
    msg = _assistant_message(output)
    return msg.get("content"), (msg.get("tool_calls") or [])


def _reconstruct_openai_chat(output: Any) -> Any:
    """Rebuild a duck-typed ChatCompletion from a recorded completion — enough for the dominant
    access pattern (`resp.choices[0].message.content` / `.tool_calls`). Not the real pydantic model;
    fields agents rarely read on replay (id/created/usage/system_fingerprint) are omitted."""
    content, raw_tcs = _extract_completion(output)
    tool_calls = [
        SimpleNamespace(
            id=tc.get("id", ""),
            type=tc.get("type", "function"),
            function=SimpleNamespace(**(tc.get("function") or {})),
        )
        for tc in raw_tcs
        if isinstance(tc, dict)
    ] or None
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


def _maybe_json(v: Any) -> Any:
    """Parse a JSON string to its value (tool args are a JSON string in the OpenAI-canonical shape
    but a dict in Anthropic's native one); pass non-strings / non-JSON through unchanged."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def _reconstruct_anthropic(output: Any) -> Any:
    """Rebuild a duck-typed Anthropic Message — enough for the dominant access pattern (iterate
    `resp.content` blocks: `.type`/`.text` for text, `.id`/`.name`/`.input` for tool_use). Handles
    both recorded shapes: native content blocks (the drop-in) and the canonical content-string +
    tool_calls (auto-instrument, after backend normalization)."""
    msg = _assistant_message(output)
    content = msg.get("content")
    blocks: list[Any] = []
    if isinstance(content, list):  # native Anthropic blocks
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                blocks.append(
                    SimpleNamespace(type="tool_use", id=b.get("id", ""), name=b.get("name", ""), input=b.get("input"))
                )
            else:
                blocks.append(SimpleNamespace(type="text", text=b.get("text", "")))
    else:  # canonical: a content string (+ OpenAI-style tool_calls → tool_use blocks)
        if content:
            blocks.append(SimpleNamespace(type="text", text=content))
        for tc in msg.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            blocks.append(
                SimpleNamespace(
                    type="tool_use",
                    id=tc.get("id", "") if isinstance(tc, dict) else "",
                    name=fn.get("name", ""),
                    input=_maybe_json(fn.get("arguments")),
                )
            )
    return SimpleNamespace(content=blocks, stop_reason=msg.get("stop_reason"), usage=None)


def _reconstruct_google(output: Any) -> Any:
    """Rebuild a duck-typed google-genai GenerateContentResponse — enough for the dominant access
    patterns (`resp.text`, `resp.function_calls`, and walking `candidates[0].content.parts`)."""
    content, raw_tcs = _extract_completion(output)
    parts: list[Any] = []
    if content:
        parts.append(SimpleNamespace(text=content, function_call=None))
    calls = []
    for tc in raw_tcs:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        call = SimpleNamespace(name=fn.get("name", ""), args=_maybe_json(fn.get("arguments")))
        calls.append(call)
        parts.append(SimpleNamespace(text=None, function_call=call))
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=parts, role="model"), finish_reason="STOP"
    )
    return SimpleNamespace(
        text=content or "", candidates=[candidate], function_calls=calls or None, usage_metadata=None
    )


def _patch_class_method(
    cls: Any,
    name: str,
    *,
    model_key: str,
    input_extractor: Callable[[dict], Any],
    reconstruct: Callable[[Any], Any],
) -> None:
    """Class-level wrap of a provider create-method: serve a fixture in replay, else call through.
    Idempotent (sentinel-guarded); handles sync + async create methods."""
    original = getattr(cls, name)
    if getattr(original, "_tracely_replay", False):
        return

    def _serve(model: str, kwargs: dict) -> tuple[bool, Any]:
        """(handled, result). handled=False → caller runs the real method (not replaying, or a
        replay miss — better a loud live failure than a silent wrong-green)."""
        store = _fixtures.get()
        if not store:
            return False, None
        inp = input_extractor(kwargs)
        entry = _pop_fixture("llm", model, inp) or _pop_fixture_any("llm")
        if entry is None:
            return False, None
        with llm(model or "") as span:
            if inp is not None:
                set_io(span, input=inp)
            span.set_attribute("tracely.replay.fixture", True)
            if entry.get("error"):
                error(span, str(entry["error"]))
                raise ToolError(str(entry["error"]))
            out = entry.get("output")
            set_io(span, output=out)
        return True, reconstruct(out)

    # Async-detection sees through functools.wraps chains (OpenAI decorates create with
    # @required_args + others — iscoroutinefunction on the outer layer returns False even when the
    # underlying method is async). Without unwrap, the async branch installs the sync wrapper and an
    # `await` on the SimpleNamespace blows up.
    is_async = inspect.iscoroutinefunction(inspect.unwrap(original))
    if is_async:

        @functools.wraps(original)
        async def traced(self: Any, *a: Any, **k: Any) -> Any:
            handled, result = _serve(k.get(model_key, "") or "", k)
            return result if handled else await original(self, *a, **k)
    else:

        @functools.wraps(original)
        def traced(self: Any, *a: Any, **k: Any) -> Any:
            handled, result = _serve(k.get(model_key, "") or "", k)
            return result if handled else original(self, *a, **k)

    traced._tracely_replay = True  # type: ignore[attr-defined]
    setattr(cls, name, traced)


def _patch_openai_replay() -> None:
    from openai.resources.chat.completions import AsyncCompletions, Completions

    for cls in (Completions, AsyncCompletions):
        _patch_class_method(
            cls,
            "create",
            model_key="model",
            input_extractor=lambda kw: kw.get("messages"),
            reconstruct=_reconstruct_openai_chat,
        )


def _patch_anthropic_replay() -> None:
    from anthropic.resources.messages import AsyncMessages, Messages

    def _inp(kw: dict) -> Any:  # fold the separate system= kwarg in, like the drop-in capture
        msgs, system = kw.get("messages"), kw.get("system")
        return [{"role": "system", "content": system}, *msgs] if system and isinstance(msgs, list) else msgs

    for cls in (Messages, AsyncMessages):
        _patch_class_method(
            cls, "create", model_key="model", input_extractor=_inp, reconstruct=_reconstruct_anthropic
        )


def _patch_google_replay() -> None:
    from google.genai.models import AsyncModels, Models

    for cls in (Models, AsyncModels):
        _patch_class_method(
            cls,
            "generate_content",
            model_key="model",
            input_extractor=lambda kw: kw.get("contents"),
            reconstruct=_reconstruct_google,
        )


def _patch_mistral_replay() -> None:
    # mistralai v1 responses are OpenAI-shaped (`choices[0].message.content`), so the OpenAI
    # reconstruction serves them as-is.
    from mistralai.chat import Chat

    for method in ("complete", "complete_async"):
        _patch_class_method(
            Chat,
            method,
            model_key="model",
            input_extractor=lambda kw: kw.get("messages"),
            reconstruct=_reconstruct_openai_chat,
        )


def _patch_module_function(
    module: Any,
    name: str,
    *,
    model_key: str,
    input_extractor: Callable[[dict], Any],
    reconstruct: Callable[[Any], Any],
) -> None:
    """Same contract as `_patch_class_method`, for a provider whose entry point is a module-level
    function (litellm) rather than a bound method — so there is no `self` to pass through."""
    original = getattr(module, name)
    if getattr(original, "_tracely_replay", False):
        return

    def _serve(model: str, kwargs: dict) -> tuple[bool, Any]:
        store = _fixtures.get()
        if not store:
            return False, None
        inp = input_extractor(kwargs)
        entry = _pop_fixture("llm", model, inp) or _pop_fixture_any("llm")
        if entry is None:
            return False, None
        with llm(model or "") as span:
            if inp is not None:
                set_io(span, input=inp)
            span.set_attribute("tracely.replay.fixture", True)
            if entry.get("error"):
                error(span, str(entry["error"]))
                raise ToolError(str(entry["error"]))
            out = entry.get("output")
            set_io(span, output=out)
        return True, reconstruct(out)

    if inspect.iscoroutinefunction(inspect.unwrap(original)):

        @functools.wraps(original)
        async def traced(*a: Any, **k: Any) -> Any:
            handled, result = _serve(k.get(model_key, "") or (a[0] if a else ""), k)
            return result if handled else await original(*a, **k)
    else:

        @functools.wraps(original)
        def traced(*a: Any, **k: Any) -> Any:
            handled, result = _serve(k.get(model_key, "") or (a[0] if a else ""), k)
            return result if handled else original(*a, **k)

    traced._tracely_replay = True  # type: ignore[attr-defined]
    setattr(module, name, traced)


def _patch_litellm_replay() -> None:
    import litellm

    for name in ("completion", "acompletion"):
        _patch_module_function(
            litellm,
            name,
            model_key="model",
            input_extractor=lambda kw: kw.get("messages"),
            reconstruct=_reconstruct_openai_chat,
        )


_REPLAY_PROVIDERS: list[Callable[[], None]] = [
    _patch_openai_replay,
    _patch_anthropic_replay,
    _patch_google_replay,
    _patch_mistral_replay,
    _patch_litellm_replay,
]


def _install_replay_patches() -> None:
    """Patch importable providers' create-methods for hermetic replay. Idempotent; called on every
    fixtures() enter. A provider that isn't installed (or whose API moved) is skipped — it just
    stays live in replay rather than breaking the others."""
    for installer in _REPLAY_PROVIDERS:
        try:
            installer()
        except Exception:  # noqa: BLE001 — provider absent / API drift; degrade to live for it
            pass
