"""MCP server — Tracely over the Model Context Protocol.

Mounted at `/mcp` on the API (streamable HTTP), so any MCP client drives a workspace with the
same ingest key the SDK already uses:

    claude mcp add --transport http tracely https://<api-host>/mcp \
        --header "Authorization: Bearer $TRACELY_KEY"

Every tool is a thin call back into our own FastAPI app over an in-process ASGI transport
(no socket, no second copy of anything). Auth, project scoping, validation and error shaping
stay in the routers, so this file adds tools without adding a second implementation of any
endpoint — and never touches a database.

Deliberately read-mostly: no delete, no admin, no ingest. Retiring a column is
`update_evaluator(enabled=False)`; sending traces is the SDK's job (hand-rolled OTLP JSON is a
trap — span ids are base64, and a wrong one silently vanishes instead of erroring).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from tracely.api.internal_client import api_call, auth_headers_from
from tracely.domain.evaluation.guide import create_evaluator_description

INSTRUCTIONS = """Tracely is trace-native CI/CD for AI agents: production traces are graded by
evaluators (the columns of the traces table), failures are clustered, and clusters become
regression cases that gate a pull request.

Useful shape of the data: a trace is one agent run (a turn); traces sharing a thread form a
conversation. A trace FAILS iff a non-advisory evaluator scored it FAIL. Evaluators are either
`structural` (deterministic checks) or `llm_judge` (a rubric prompt graded by a model)."""

mcp = FastMCP(
    "tracely",
    instructions=INSTRUCTIONS,
    # Every tool authenticates per-request from the caller's header; there is no per-session
    # state worth keeping, and stateless lets the API scale to more than one replica.
    stateless_http=True,
    # FastMCP defaults to DNS-rebinding protection with an allow-list of localhost only, which
    # 421s every request to a deployed API (`Invalid Host header: api.example.com`). That
    # protection exists for MCP servers bound to a developer's loopback interface, where a
    # malicious page can reach the origin and the browser attaches ambient credentials. This
    # endpoint has no ambient credentials to steal — every call carries an explicit Bearer key,
    # and CORS already pins which origins a browser may read from.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def _call(ctx: Context, method: str, path: str, ndjson: bool = False, **kw: Any) -> Any:
    """Issue `method path` against our own FastAPI app, as the caller.

    The MCP client's `Authorization` (an ingest key, or a session JWT + `x-tracely-project`) is
    forwarded verbatim — the router's own `get_project_id` dependency is what resolves the
    project, so an MCP caller can never reach a workspace its key doesn't own.
    """
    request = getattr(ctx.request_context, "request", None)
    headers = auth_headers_from(request.headers if request is not None else {})
    if not headers:
        raise ValueError(
            "missing credentials: pass your Tracely ingest key as an Authorization: Bearer header "
            "when configuring this MCP server"
        )
    return await api_call(headers, method, path, ndjson=ndjson, **kw)


# ── traces ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_traces(ctx: Context, limit: int = 20) -> list[dict]:
    """Recent traces (newest first) with their verdict, agent, latency, cost and token counts.

    One row per agent run. Start here to answer "what is my agent doing / what is failing".
    """
    return await _call(ctx, "GET", "/api/traces", params={"limit": limit})


@mcp.tool()
async def get_trace(ctx: Context, trace_id: str) -> dict:
    """One trace in full: every span (nested by parent), its inputs/outputs, and every
    evaluator score attached to it. This is the evidence for why a run passed or failed."""
    return await _call(ctx, "GET", f"/api/traces/{trace_id}")


@mcp.tool()
async def search_traces(ctx: Context, query: str) -> list[dict]:
    """Find conversations whose user message contains `query` (min 2 characters). Also matches
    registry objects (agents, cases, clusters) by name."""
    return await _call(ctx, "GET", "/api/search", params={"q": query})


# Whole conversations are the largest thing this server can return — every turn, every span,
# every score. A workspace-sized export would evict the rest of the caller's context, so the tool
# caps what a single call can pull; narrow with `meta`/`from_ts` rather than raising it.
_EXPORT_MAX = 25


@mcp.tool()
async def export_conversations(
    ctx: Context,
    limit: int = 5,
    meta: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict]:
    """Whole conversations, newest first — each with its turns, the spans behind every turn, and
    the scores at both levels. `list_traces` answers "what ran"; this answers "what was actually
    said, step by step".

    `meta` filters on span metadata as `key=value` (e.g. `business_id=2a73b883-…`) — the way to
    pull one tenant's traffic out of a shared workspace. `from_ts`/`to_ts` are ISO-8601 UTC bounds
    on the trace start. `limit` is capped at 25 conversations per call: these payloads are large,
    so narrow the window instead of asking for more.
    """
    params: dict[str, Any] = {"limit": max(1, min(limit, _EXPORT_MAX))}
    for k, v in (("meta", meta), ("from_ts", from_ts), ("to_ts", to_ts)):
        if v:
            params[k] = v
    return await _call(ctx, "GET", "/api/export", ndjson=True, params=params)


# ── evaluators (the columns of the traces table) ──────────────────────────────


@mcp.tool()
async def list_evaluators(ctx: Context) -> list[dict]:
    """Every evaluator configured in this workspace — id, kind, level, score_name, enabled,
    and its full config. Each one is a column in the traces table."""
    return await _call(ctx, "GET", "/api/evaluators")


@mcp.tool()
async def list_evaluator_templates(ctx: Context) -> list[dict]:
    """The catalog of ready-made evaluators, each with a proven `config`, flagged with whether
    this workspace already installed it. Prefer copying a template's config into
    `create_evaluator` over inventing one from scratch."""
    return await _call(ctx, "GET", "/api/evaluators/templates")


@mcp.tool(description=create_evaluator_description(
    "config", "backfill existing conversations with Run in the traces table."
))
async def create_evaluator(
    ctx: Context,
    name: str,
    config: dict,
    kind: str = "llm_judge",
    level: str = "AGENT_RUN",
    description: str = "",
    score_name: str = "",
    enabled: bool = True,
) -> dict:
    body = {
        "name": name,
        "description": description,
        "kind": kind,
        "level": level,
        "enabled": enabled,
        "config": config,
        "score_name": score_name,
    }
    return await _call(ctx, "POST", "/api/evaluators", json=body)


@mcp.tool()
async def update_evaluator(
    ctx: Context,
    evaluator_id: str,
    name: str | None = None,
    description: str | None = None,
    level: str | None = None,
    enabled: bool | None = None,
    config: dict | None = None,
    target_agent: str | None = None,
    target_env: str | None = None,
    sampling: float | None = None,
) -> dict:
    """Patch an evaluator — only the fields you pass change. `enabled=False` retires a column
    without losing the scores it already produced. `config` REPLACES the whole config: read it
    with list_evaluators, change what you need (e.g. add `run_if`), send all of it back. `sampling` (0.0–1.0) grades a deterministic
    fraction of traces; `target_agent` / `target_env` scope it to one agent or environment."""
    patch = {
        k: v
        for k, v in {
            "name": name,
            "description": description,
            "level": level,
            "enabled": enabled,
            "config": config,
            "target_agent": target_agent,
            "target_env": target_env,
            "sampling": sampling,
        }.items()
        if v is not None
    }
    return await _call(ctx, "PATCH", f"/api/evaluators/{evaluator_id}", json=patch)


# ── failure intelligence & trends ─────────────────────────────────────────────


@mcp.tool()
async def list_clusters(ctx: Context, limit: int = 20, min_size: int | None = None) -> dict:
    """Failure clusters (biggest first): recurring failure modes grouped by semantic similarity,
    with a title, status and member count. The fastest read on what is actually broken."""
    params: dict[str, Any] = {"limit": limit}
    if min_size is not None:
        params["min_size"] = min_size
    return await _call(ctx, "GET", "/api/clusters", params=params)


@mcp.tool()
async def get_cluster(ctx: Context, cluster_id: str, members: int = 25) -> dict:
    """One failure cluster in detail: its analysis, its suggested fix, and the traces that
    belong to it — the raw material for a regression case."""
    return await _call(ctx, "GET", f"/api/clusters/{cluster_id}", params={"members": members})


@mcp.tool()
async def get_trends(ctx: Context, days: int = 14) -> dict:
    """Daily traces vs failures over `days` (1–90), plus gate and cluster roll-ups."""
    return await _call(ctx, "GET", "/api/trends", params={"days": days})


@mcp.tool()
async def get_ops_metrics(ctx: Context, days: int = 14) -> dict:
    """Latency (p50/p95), throughput, token and cost roll-up over `days` (1–90)."""
    return await _call(ctx, "GET", "/api/ops", params={"days": days})


class _ASGIHandler:
    """Adapter so Starlette treats the MCP endpoint as a raw ASGI app: a bare function or bound
    method in a `Route` is taken to be `func(request) -> response`, and only a non-function
    object is handed the ASGI three-tuple untouched. Resolves the session manager per call
    rather than capturing it, so the route still works if the manager is rebuilt (tests do).
    """

    async def __call__(self, scope, receive, send):
        await mcp.session_manager.handle_request(scope, receive, send)


def mount(app) -> None:
    """Serve the MCP endpoint at exactly `/mcp` on `app`.

    Deliberately not `app.mount("/mcp", mcp.streamable_http_app())`, the obvious spelling: a
    Starlette Mount only matches `/mcp/...`, so `POST /mcp` answers 307 -> `/mcp/` — and the
    official MCP Python client does not follow redirects, so the handshake fails outright for
    anyone using it. An exact route to the session manager's ASGI handler serves `/mcp` itself.

    `streamable_http_app()` is still called: it is what constructs the session manager, which
    the API's `lifespan` has to run for the transport to work at all.
    """
    from starlette.routing import Route

    mcp.streamable_http_app()
    app.router.routes.append(
        Route(
            "/mcp",
            _ASGIHandler(),
            methods=["GET", "POST", "DELETE"],
            name="mcp",
        )
    )
