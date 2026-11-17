"""Real environments for audit rollouts. `get_backend()` is the only entry point."""

from __future__ import annotations

from tracely.config import settings
from tracely.infrastructure.sandbox.base import (
    ExecResult,
    SandboxBackend,
    SandboxError,
    SandboxGone,
    SandboxSpec,
)


def get_backend() -> SandboxBackend | None:
    """The configured backend, or None — in which case audits can only invent tool results.
    A deployment without one is exactly like a deployment without an LLM key: the feature
    degrades, it does not break."""
    if settings.sandbox_backend == "docker":
        from tracely.infrastructure.sandbox.docker import DockerBackend

        return DockerBackend()
    return None


__all__ = ["ExecResult", "SandboxBackend", "SandboxError", "SandboxGone", "SandboxSpec", "get_backend"]
