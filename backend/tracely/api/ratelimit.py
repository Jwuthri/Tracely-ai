"""Per-IP rate limit for the endpoints anyone on the internet can hit without a credential:
login, register, forgot/reset password, accept invite. Argon2 makes one guess slow; nothing
else made a million guesses slow.

A fixed window in Redis (`INCR` + `EXPIRE`), keyed by route and client IP. Fails OPEN when Redis
is unreachable: this is a brake on guessing, not an auth boundary, and an outage must not lock
everyone out of signing in. The client IP is the first `X-Forwarded-For` hop when present (the
deployment runs behind a proxy), which a direct caller can spoof — good enough for a brake.

    @router.post("/auth/login", dependencies=[Depends(limit("login", 20))])

ponytail: fixed window, one Redis round-trip. A sliding window or per-account lockout is the
upgrade if credential stuffing ever shows up in the logs."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from tracely.infrastructure.redis_client import sync_redis

log = structlog.get_logger()

WINDOW_SECONDS = 60


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def hits(name: str, ip: str) -> int | None:
    """Count this request and return the window's total, or None when Redis is unavailable."""
    key = f"tracely:rl:{name}:{ip}"
    try:
        r = sync_redis()
        n = r.incr(key)
        if n == 1:
            r.expire(key, WINDOW_SECONDS)
        return int(n)
    except Exception as exc:  # noqa: BLE001 — fail open, see module docstring
        log.warning("ratelimit_unavailable", error=str(exc))
        return None


def limit(name: str, per_minute: int) -> Callable[..., Coroutine[Any, Any, None]]:
    async def _dep(request: Request) -> None:
        n = await run_in_threadpool(hits, name, client_ip(request))  # sync redis, off the loop
        if n is not None and n > per_minute:
            raise HTTPException(
                status_code=429,
                detail="too many attempts — try again in a minute",
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )

    return _dep
