"""The sandbox seam: where a rollout's real environment lives.

Six verbs, and nothing above this module knows which backend answers them — a local Docker
daemon today, a hosted microVM API next (`SANDBOX_BACKEND`). Files move as tar archives because
every backend has that primitive and it is binary-safe; snapshots are just `get` of the home
directory, so rollback works the same on all of them.

A sandbox is **disposable and owned by reference**. The worker never holds one in memory across
ticks: any tick finds it by `ref`, and if it is gone (`SandboxGone` — a host restart, the
lifetime cap) rebuilds it from the rollout's last snapshot. That is also what lets two worker
replicas share a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

LABEL_OWNER = "tracely.sandbox.owner"
LABEL_ROLLOUT = "tracely.sandbox.rollout"


class SandboxError(RuntimeError):
    """The backend could not do what was asked."""


class SandboxGone(SandboxError):
    """The sandbox no longer exists. Recoverable: rebuild it from the last snapshot."""


@dataclass
class SandboxSpec:
    """What a sandbox is created from. Built by Tracely ALONE — the image comes from the
    deployment's setting and nothing in here is ever taken from a request, a seed or a model:
    whoever controls this controls a container on the host."""

    image: str
    hostname: str
    owner: str
    rollout_id: str
    user: str = "dev"
    home: str = "/home/dev"
    # The account the business services run as. Its home holds their state; the audited model's
    # user cannot read it — it meets those services only as tools.
    service_user: str = "svc"
    service_home: str = "/home/svc"
    # "" = no network at all. Otherwise the name of the INTERNAL network whose only other member
    # is the egress proxy — never a network with a route of its own.
    network: str = ""
    lifetime_s: int = 3600
    memory: str = "1g"
    cpus: float = 1.0
    pids: int = 256
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int
    output: bytes  # stdout and stderr interleaved; UNTRUSTED bytes, already size-capped


class SandboxBackend(Protocol):
    def create(self, spec: SandboxSpec) -> str: ...

    def exec(
        self, ref: str, argv: list[str], *, user: str, workdir: str, env: dict[str, str]
    ) -> ExecResult: ...

    def put(self, ref: str, path: str, tar: bytes) -> None:
        """Extract a tar archive at `path` inside the sandbox."""

    def get(self, ref: str, path: str, max_bytes: int) -> bytes | None:
        """`path` as a tar archive, or None when it is larger than `max_bytes`."""

    def destroy(self, ref: str) -> None: ...

    def list(self, owner: str) -> list[dict]:
        """`[{ref, rollout_id}]` for every sandbox this deployment created."""
