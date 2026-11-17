"""Tracely API entrypoint — `uvicorn tracely.api.main:app`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tracely.api.routers import (
    admin,
    analytics,
    audits,
    assistant,
    billing,
    cases,
    calibration,
    clusters,
    evaluations,
    evaluators,
    gate,
    health,
    meta_analysis,
    monitors,
    otlp,
    search,
    sessions,
    share,
    traces,
    scenarios,
)
from sqlalchemy import select

from tracely.api import mcp_server
from tracely.api.routers import auth as auth_router
from tracely.auth import AuthError
from tracely.config import settings
from tracely.infrastructure import spans
from tracely.infrastructure.db.engine import AsyncSessionLocal, async_engine, sync_engine
from tracely.infrastructure.db.models import IngestKey
from tracely.log_config import configure_logging
from tracely.services.seeding_service import DEFAULT_KEY

log = structlog.get_logger()


def _warm_pricing() -> None:
    """Lazy import: `provider` pulls the LLM stack, which must stay off the import path of a
    deployment that has no model configured (see the `llm_enabled()` rule)."""
    from tracely.infrastructure.llm.provider import warm_pricing_catalog

    warm_pricing_catalog()


def _init_sentry() -> None:
    """Optional Sentry init — no-op when `SENTRY_DSN` is unset or `sentry-sdk` isn't installed
    (we don't pin sentry-sdk as a hard dependency; operators add it in prod via `uv add sentry-sdk`)."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
    except ImportError:
        log.warning("sentry_skipped_no_sdk", hint="pip install sentry-sdk")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.tracely_env,
        integrations=[FastApiIntegration()],
        # Default conservative — operators tune via the Sentry project UI / their own wrapper.
        traces_sample_rate=0.0,
    )


async def _refuse_dev_key_in_prod() -> None:
    """Defense in depth: even if the operator forgot to scrub the seed key, refuse to boot in prod
    when `tracely_dev_key` is still a valid IngestKey. The seeding service won't create it in prod
    going forward, but pre-existing DBs need this fail-fast at startup."""
    if not settings.is_prod:
        return
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(IngestKey).where(IngestKey.key == DEFAULT_KEY))
        ).scalar_one_or_none()
    if row is not None:
        raise RuntimeError(
            f"TRACELY_ENV=prod but the seeded dev ingest key '{DEFAULT_KEY}' is still active "
            f"(project_id={row.project_id}). Delete that IngestKey row before booting."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    _init_sentry()
    log.info("api_startup", env=settings.tracely_env, auth_mode=settings.auth_mode)
    await _refuse_dev_key_in_prod()
    # Model prices come from OpenRouter's published catalog, not a list we maintain. Loaded in the
    # background — a 10s network timeout must never hold up the health check.
    asyncio.create_task(asyncio.to_thread(_warm_pricing))
    # The MCP endpoint's streamable-HTTP transport needs its session manager running for the
    # lifetime of the app; it has no lifespan of its own, so this is the only place to run it.
    async with mcp_server.mcp.session_manager.run():
        yield
    # Release pooled connections so reloads/restarts/shutdowns don't leak sockets + file descriptors.
    if not spans.POSTGRES:
        from tracely.infrastructure.clickhouse.client import close_async_client

        await close_async_client()
    await async_engine.dispose()
    sync_engine.dispose()


app = FastAPI(title="Tracely API", version="0.1.0", lifespan=lifespan)

# The web app fetches the API via same-origin Next proxy routes. Allow direct browser calls from a
# local dev frontend on any port (dev/staging only — never punch a localhost hole in prod), plus the
# hosted frontend origin when configured (CORS for SaaS).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin] if settings.frontend_origin else [],
    allow_origin_regex=None if settings.is_prod else r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AuthError)
async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: log the unhandled error with request context (instead of a bare 500 with
    no trace) and return a generic body that never leaks internals to the client."""
    log.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


app.include_router(health.router)
app.include_router(otlp.router)
app.include_router(traces.router)
app.include_router(sessions.router)
app.include_router(search.router)
app.include_router(cases.router)
app.include_router(gate.router)
app.include_router(scenarios.router)
app.include_router(audits.router)
app.include_router(clusters.router)
app.include_router(analytics.router)
app.include_router(assistant.router)
app.include_router(evaluators.router)
app.include_router(evaluations.router)
app.include_router(meta_analysis.router)
app.include_router(calibration.router)
app.include_router(monitors.router)
app.include_router(share.router)
app.include_router(admin.router)
# Always mounted, even for self-hosters: /api/billing/usage answers `billing_enabled: false`
# rather than 404ing the frontend's unconditional probe; the Stripe endpoints gate themselves.
app.include_router(billing.router)

# Auth: /auth/me + /auth/logout always; mode-specific endpoints gated by AUTH_MODE.
app.include_router(auth_router.common_router)
if settings.auth_mode == "local":
    app.include_router(auth_router.local_router)
elif settings.auth_mode == "clerk":
    app.include_router(auth_router.clerk_router)

# MCP (streamable HTTP) — the same API, driven by an agent. Authenticated per-request by the
# caller's own Bearer key, exactly like every router above. Wired at import so the session
# manager exists by the time `lifespan` runs it.
mcp_server.mount(app)
