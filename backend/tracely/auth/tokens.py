"""Local-mode session tokens: HS256 JWTs signed with SESSION_SECRET.

Security: the algorithm is PINNED to HS256 on verify (defeats alg-confusion forgery), the issuer is
checked, and exp/iss/sub are required. The secret never leaves the backend; the frontend only forwards
the opaque token in `Authorization: Bearer`.

Revocation: a token carries the issuing user's `token_version` as `tv`, and `resolve_principal`
rejects it when the stored counter has moved on. Bumping that counter (password change, password
reset) is the only way to end a session before it expires — there is no denylist and nothing to
sweep. A token minted before `tv` existed has no claim, reads as 0, and still matches the column
default, so shipping this signs nobody out."""

from __future__ import annotations

import time

import jwt

from tracely.config import settings


class TokenError(Exception):
    """Raised when a session token is malformed, expired, or fails verification."""


def issue_session(
    user_id: str, *, ttl_seconds: int | None = None, token_version: int = 0
) -> str:
    now = int(time.time())
    ttl = settings.session_ttl_seconds if ttl_seconds is None else ttl_seconds
    payload = {
        "sub": user_id,
        "iss": settings.session_issuer,
        "iat": now,
        "exp": now + ttl,
        "tv": int(token_version or 0),
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def verify_session(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.session_secret,
            algorithms=["HS256"],
            issuer=settings.session_issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as e:  # expired, bad sig, wrong issuer, missing claim, alg mismatch …
        raise TokenError(str(e)) from e


# ── public share links ────────────────────────────────────────────────────────

# A share token is a read-only capability for exactly ONE object — not a login. It carries its own
# issuer and deliberately NO `sub`, so it can never satisfy `verify_session` (which requires both),
# and it must never be handed to `resolve_principal`: doing so would promote a share link into a
# full project read key. `api/routers/share.py` verifies it directly for that reason.
SHARE_ISSUER = "tracely-share"
SHARE_TTL_SECONDS = 30 * 24 * 3600

# The subjects a link may point at. A token names one of these plus one id — never a project, never
# a list. Adding a kind means adding a reader in `share.py`; an unknown kind reads as 404 there.
SHARE_KINDS = ("conversation", "gate")


def issue_share(
    project_id: str,
    subject_id: str,
    *,
    kind: str = "conversation",
    ttl_seconds: int = SHARE_TTL_SECONDS,
) -> str:
    now = int(time.time())
    payload = {
        "pid": project_id,
        "kind": kind,
        "sid": subject_id,
        "iss": SHARE_ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def verify_share(token: str) -> dict:
    """`{project_id, kind, subject_id, issued_at, expires_at}` for a valid token; else `TokenError`.

    Tokens minted before this carried the conversation id as `tid` and no `kind`, so those two
    claims are read with the old names as a fallback — links already pasted into a Slack thread
    keep resolving. A token with neither claim is rejected rather than falling through to "the
    whole project".

    Revocation is NOT checked here (this module does no I/O): the caller compares `issued_at`
    against the subject's revocation timestamp — see `api/routers/share.py`.
    """
    try:
        claims = jwt.decode(
            token,
            settings.session_secret,
            algorithms=["HS256"],
            issuer=SHARE_ISSUER,
            options={"require": ["exp", "iss", "pid"]},
        )
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e
    subject_id = claims.get("sid") or claims.get("tid")
    if not subject_id:
        raise TokenError("share token names no subject")
    return {
        "project_id": claims["pid"],
        "kind": claims.get("kind") or "conversation",
        "subject_id": subject_id,
        # A token with no `iat` reads as epoch 0, so any revocation kills it. Fail closed.
        "issued_at": int(claims.get("iat") or 0),
        "expires_at": int(claims["exp"]),
    }
