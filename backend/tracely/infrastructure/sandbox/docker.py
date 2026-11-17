"""Sandboxes as sibling containers on the worker's Docker daemon.

For local development and single-host self-hosting (compose, Coolify). It needs the daemon's
socket, which makes the worker root-equivalent on the host — acceptable where the operator and
the host are the same party, never on a multi-tenant deployment (`config.py` refuses to boot
that combination). The hosted backend is the answer there.

The container is as inert as Docker can make one: **no network at all**, every capability
dropped, no privilege escalation, a non-root user, bounded memory, CPU and process count, no
bind mounts — and a main process that is nothing but `sleep <lifetime>` with auto-remove, so a
sandbox outlives a crashed Tracely by at most its lifetime with nobody having to reap it.
"""

from __future__ import annotations

from typing import Any

from tracely.infrastructure.sandbox.base import (
    LABEL_OWNER,
    LABEL_ROLLOUT,
    ExecResult,
    SandboxError,
    SandboxGone,
    SandboxSpec,
)


def container_kwargs(spec: SandboxSpec) -> dict[str, Any]:
    """The `containers.run` arguments for a spec. Its own function so the security posture is a
    value a test can assert on, not something to be inferred from a mock."""
    return {
        "image": spec.image,
        # The whole lifecycle guarantee: PID 1 exits at the lifetime cap and the container
        # removes itself — the one cleanup that works with Tracely down.
        "command": ["sleep", str(int(spec.lifetime_s))],
        "detach": True,
        "auto_remove": True,
        "init": True,  # reap what the target's commands leave behind
        # No network — or, for an allowlisted audit, the internal network that holds nothing but
        # the egress proxy. There is no third option, and no way to ask for one.
        **({"network": spec.network} if spec.network else {"network_mode": "none"}),
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "user": spec.user,
        "working_dir": spec.home,
        "hostname": spec.hostname,
        "mem_limit": spec.memory,
        "nano_cpus": int(spec.cpus * 1_000_000_000),
        "pids_limit": spec.pids,
        # Nothing inherited: the image's own ENV would otherwise name how it was built.
        "environment": {"HOME": spec.home, "USER": spec.user, "LANG": "C.UTF-8"},
        "labels": {**spec.labels, LABEL_OWNER: spec.owner, LABEL_ROLLOUT: spec.rollout_id},
    }


class DockerBackend:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import docker  # lazy: only a deployment that enabled this backend needs the SDK

            self._client = docker.from_env()
        return self._client

    def _container(self, ref: str):
        from docker.errors import NotFound

        try:
            return self.client.containers.get(ref)
        except NotFound as exc:
            raise SandboxGone(f"sandbox {ref[:12]} no longer exists") from exc

    def _call(self, fn, *args, **kwargs):
        from docker.errors import APIError, NotFound

        try:
            return fn(*args, **kwargs)
        except NotFound as exc:
            raise SandboxGone(str(exc)) from exc
        except APIError as exc:
            # 409 = the container exists but is not running (its lifetime ran out mid-call).
            if exc.status_code == 409:
                raise SandboxGone(str(exc)) from exc
            raise SandboxError(str(exc)) from exc

    def create(self, spec: SandboxSpec) -> str:
        return self._call(self.client.containers.run, **container_kwargs(spec)).id

    def exec(self, ref, argv, *, user, workdir, env) -> ExecResult:
        container = self._container(ref)
        # `env -i`, not exec's own `environment=`: Docker ADDS that to the image's variables, and
        # a base image carries its build's (PYTHON_SHA256, GPG_KEY, …) into every command. The
        # contract is that `env` is the whole environment.
        clean = ["env", "-i", *(f"{k}={v}" for k, v in env.items()), *argv]
        code, output = self._call(container.exec_run, clean, user=user, workdir=workdir)
        return ExecResult(exit_code=int(code if code is not None else -1), output=output or b"")

    def put(self, ref, path, tar) -> None:
        self._call(self._container(ref).put_archive, path, tar)

    def get(self, ref, path, max_bytes):
        stream, _stat = self._call(self._container(ref).get_archive, path)
        chunks, size = [], 0
        for chunk in stream:
            size += len(chunk)
            if size > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def destroy(self, ref) -> None:
        from docker.errors import APIError, NotFound

        try:
            self.client.containers.get(ref).remove(force=True)
        except (NotFound, APIError):
            pass  # already gone, or mid-removal by its own auto-remove

    def list(self, owner):
        found = self.client.containers.list(all=True, filters={"label": f"{LABEL_OWNER}={owner}"})
        return [{"ref": c.id, "rollout_id": (c.labels or {}).get(LABEL_ROLLOUT, "")} for c in found]
