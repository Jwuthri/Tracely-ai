"""One rollout's hold on its real environment.

Turns the target's tool calls and the attacker's setup calls into sandbox commands, and owns the
two things that make a real environment work with rollback and with ticks:

- **Snapshots.** The home directory as a tar archive, stored in the blob store. A rollback to a
  message restores the snapshot taken after it; a sandbox that disappeared between ticks is
  rebuilt from the last one. Restore is *destroy, create, extract* — never "clean up in place" —
  so stray processes and anything written outside the home directory are reset with it.
- **Nothing in memory.** The session is rebuilt from `audit_rollouts.sandbox` on every tick.

Everything read back from the sandbox is untrusted bytes: capped at the source, decoded
leniently, and a snapshot is never unpacked on the worker — only handed back to a sandbox.
"""

from __future__ import annotations

import gzip
import json
import posixpath
import secrets
import time
from dataclasses import dataclass

import structlog

from tracely.config import settings
from tracely.domain.audit import media, tooling
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.sandbox import SandboxBackend, SandboxError, SandboxGone, SandboxSpec

log = structlog.get_logger()

SNAPSHOT_CAP = 64 * 1024 * 1024  # a home directory larger than this is not snapshotted
_SNAPSHOT_ROOT = "/home"
# Where the image keeps the business services (`domain/audit/services.py`, copied in by
# Dockerfile.sandbox). Readable only by the service account.
_SERVICES_ENTRY = "/usr/local/lib/svc/tools.py"
_DEFAULT_TIMEOUT_S = 60
_MAX_TIMEOUT_S = 300


@dataclass
class ToolOutcome:
    content: str
    status: str  # "success" | "error"
    cwd: str
    file: tuple[bytes, str] | None = None  # (bytes, name): an image the tool loaded for the target


class SandboxSession:
    def __init__(self, backend: SandboxBackend, *, project_id: str, rollout_id: str,
                 record: dict | None = None, network: str = "none") -> None:
        # "allowlist" only means something where the deployment has an egress network; without
        # one the sandbox simply has no network — the safe reading of a missing proxy.
        self.proxied = network == "allowlist" and bool(settings.sandbox_egress_network)
        self.backend = backend
        self.project_id = project_id
        self.rollout_id = rollout_id
        self.record: dict = dict(record or {})  # persisted: {ref, snapshots}
        self.last_handler_error = ""

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _spec(self) -> SandboxSpec:
        return SandboxSpec(
            image=settings.sandbox_image,
            # A plausible machine name, stable for the rollout so a rebuilt sandbox is "the same box".
            hostname=f"dev-{self.rollout_id.replace('-', '')[:6]}",
            owner=settings.sandbox_owner, rollout_id=self.rollout_id,
            lifetime_s=settings.sandbox_lifetime_s,
            network=settings.sandbox_egress_network if self.proxied else "",
        )

    @property
    def home(self) -> str:
        return self._spec().home

    def ensure(self, snapshot_key: str = "") -> None:
        """Make sure a sandbox exists. Creating one — the first time, or because the last one is
        gone — starts it from `snapshot_key` when given."""
        if self.record.get("ref"):
            return
        self._create(snapshot_key)

    def _create(self, snapshot_key: str) -> None:
        spec = self._spec()
        ref = self.backend.create(spec)
        self.record["ref"] = ref
        if snapshot_key:
            archive = gzip.decompress(blobstore.get_blob(snapshot_key))
            self.backend.put(ref, "/", archive)  # the archive's root is `home/`
        log.info("sandbox_created", rollout_id=self.rollout_id, restored=bool(snapshot_key))

    def restore(self, snapshot_key: str) -> None:
        """Rewind the environment: a new sandbox from the snapshot (or a pristine one)."""
        self.destroy()
        self._create(snapshot_key)

    def destroy(self) -> None:
        ref = self.record.pop("ref", None)
        if ref:
            try:
                self.backend.destroy(ref)
            except SandboxError as exc:  # the lifetime cap is the backstop
                log.warning("sandbox_destroy_failed", rollout_id=self.rollout_id, error=str(exc))

    def snapshot(self) -> str:
        """Store the home directory and return its blob key — "" when it is too large (the
        rollout then rewinds to an older snapshot rather than failing)."""
        # All of /home: the target's files AND the services' state (the mail it sent, the refund
        # it issued) rewind together.
        archive = self.backend.get(self.record["ref"], _SNAPSHOT_ROOT, SNAPSHOT_CAP)
        if archive is None:
            log.warning("sandbox_snapshot_too_large", rollout_id=self.rollout_id)
            return ""
        n = int(self.record.get("snapshots") or 0) + 1
        self.record["snapshots"] = n
        key = blobstore.audit_blob_key(self.project_id, f"snapshots/{self.rollout_id}/{n}.tgz")
        blobstore.put_blob(key, gzip.compress(archive, compresslevel=3), "application/gzip")
        return key

    # ── running things ────────────────────────────────────────────────────────

    def _exec(self, argv: list[str], cwd: str = "", service: bool = False):
        spec = self._spec()
        user, home = (spec.service_user, spec.service_home) if service else (spec.user, spec.home)
        return self.backend.exec(
            self.record["ref"], argv, user=user, workdir=cwd or home,
            # Scrubbed on every call: what a command sees is this and nothing the image carried.
            env={"HOME": home, "USER": user, "LANG": "C.UTF-8", "TERM": "xterm-256color",
                 "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 **self._proxy_env(service)},
        )

    def _proxy_env(self, service: bool) -> dict[str, str]:
        """The proxy settings a corporate machine carries — for the target's user only. The
        business services never need the outside."""
        if not self.proxied or service:
            return {}
        url = settings.sandbox_proxy_url
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
                "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}

    def _svc(self, *args: str) -> dict:
        """One request to the business services, as their own account. `{ok, result | error}`."""
        result = self._exec(["python3", _SERVICES_ENTRY, *args], service=True)
        try:
            out = json.loads(result.output.decode("utf-8", "replace"))
            return out if isinstance(out, dict) else {"ok": False, "error": "malformed response"}
        except ValueError:
            return {"ok": False, "error": result.output.decode("utf-8", "replace")[-400:] or "no response"}

    def _service_tool(self, name: str, args: dict, cwd: str, handler: str = "") -> ToolOutcome | None:
        """A business tool call. None means "the attacker's implementation failed" — the caller
        hands that one call back to the attacker instead of showing the target an error of ours."""
        if handler:
            spec = self._spec()
            self._exec(["mkdir", "-p", f"{spec.service_home}/handlers"], service=True)
            self.backend.put(self.record["ref"], f"{spec.service_home}/handlers",
                             tooling.file_tar(f"{name}.py", handler.encode("utf-8"), uid=1001, gid=1001, mtime=int(time.time())))
            out = self._svc("handler", name, json.dumps(args))
            if out.get("handler_error"):
                self.last_handler_error = str(out.get("error") or "")
                return None
        else:
            out = self._svc("call", name, json.dumps(args))
        if out.get("ok"):
            return ToolOutcome(json.dumps(out.get("result"), indent=2, ensure_ascii=False, default=str)[:tooling.OUTPUT_CAP], "success", cwd)
        return ToolOutcome(json.dumps({"error": out.get("error") or "request failed"}), "error", cwd)

    def _bash(self, command: str, cwd: str, timeout_s: int) -> ToolOutcome:
        timeout_s = min(max(int(timeout_s or _DEFAULT_TIMEOUT_S), 1), _MAX_TIMEOUT_S)
        marker = f"__{secrets.token_hex(6)}__"  # per call, so command output cannot forge it
        result = self._exec(tooling.bash_argv(command, cwd, self.home, timeout_s, marker))
        text, rc, new_cwd = tooling.parse_bash(result.output, marker, timeout_s)
        if rc not in (0, None):
            text = f"{text}\n\n[exit code {rc}]" if text.strip() else f"[exit code {rc}]"
        return ToolOutcome(text or "(no output)", "success" if rc == 0 else "error", new_cwd or cwd or self.home)

    def _read(self, path: str, cwd: str) -> tuple[str | None, str]:
        result = self._exec(["head", "-c", str(tooling.FILE_CAP), "--", path], cwd)
        text = result.output.decode("utf-8", "replace")
        return (text, "") if result.exit_code == 0 else (None, text.strip() or "could not read the file")

    def _write(self, path: str, content: str, cwd: str) -> str:
        """Returns "" on success, else why not."""
        parent = posixpath.dirname(path)
        made = self._exec(["mkdir", "-p", "--", parent], cwd)
        if made.exit_code != 0:
            return made.output.decode("utf-8", "replace").strip() or "could not create the directory"
        try:
            self.backend.put(
                self.record["ref"], parent,
                # Stamped now: a file dated 1970 is the kind of detail that gives a machine away.
                tooling.file_tar(posixpath.basename(path), content.encode("utf-8"), mtime=int(time.time())),
            )
        except SandboxGone:
            raise
        except SandboxError as exc:
            return str(exc)[:300]
        return ""

    def run_tool(self, name: str, args: dict, cwd: str, tool: dict | None = None) -> ToolOutcome | None:
        """One of the target's real tool calls. A bad argument is the target's error to read, in
        the tool's own voice — never an exception, never anything that names this harness."""
        cwd = cwd or self.home
        if tool and (tool.get("service") or tool.get("handler")):
            return self._service_tool(name, args, cwd, handler=str(tool.get("handler") or ""))
        if name == "bash":
            return self._bash(str(args.get("command") or ""), cwd, args.get("timeout") or 0)
        path = tooling.resolve(str(args.get("path") or ""), cwd, self.home)
        if name == "view_image":
            data = self.fetch(path)
            if data is None or not media.sniff(data).startswith("image/"):
                return ToolOutcome(f"{path}: not an image file, or too large to open", "error", cwd)
            return ToolOutcome(f"Opened {path} ({len(data)} bytes).", "success", cwd, (data, posixpath.basename(path)))
        if name == "read_file":
            text, why = self._read(path, cwd)
            if text is None:
                return ToolOutcome(why, "error", cwd)
            return ToolOutcome(tooling.numbered(text, args.get("offset"), args.get("limit")) or "(empty file)", "success", cwd)
        if name == "write_file":
            why = self._write(path, str(args.get("content") or ""), cwd)
            return ToolOutcome(why or f"Wrote {path}", "error" if why else "success", cwd)
        if name == "edit_file":
            text, why = self._read(path, cwd)
            if text is None:
                return ToolOutcome(why, "error", cwd)
            updated, why = tooling.replace_once(text, str(args.get("old_string") or ""), str(args.get("new_string") or ""))
            if updated is None:
                return ToolOutcome(why, "error", cwd)
            why = self._write(path, updated, cwd)
            return ToolOutcome(why or f"Edited {path}", "error" if why else "success", cwd)
        return ToolOutcome(f"unknown tool: {name}", "error", cwd)

    def fetch(self, path: str) -> bytes | None:
        """A file's bytes out of the sandbox — None when it is missing or over the media cap."""
        path = tooling.resolve(path, self.home, self.home)
        try:
            archive = self.backend.get(self.record["ref"], path, media.MAX_BYTES + 4096)
        except SandboxGone:
            raise
        except SandboxError:
            return None
        return tooling.tar_member(archive) if archive else None

    def run_env(self, name: str, args: dict) -> str:
        """One of the ATTACKER's setup calls. Same machine, same user as the target — whatever it
        builds is exactly what the target will find."""
        if name == "env_write_file":
            path = tooling.resolve(str(args.get("path") or ""), self.home, self.home)
            why = self._write(path, str(args.get("content") or ""), self.home)
            return f"Error: {why}" if why else f"Wrote {path}"
        if name == "env_load_world":
            opts = {k: args[k] for k in ("company", "me") if args.get(k)}
            return json.dumps(self._svc("world", str(args.get("world") or "office"), json.dumps(opts)))
        if name == "env_seed":
            rows = args.get("rows")
            if isinstance(rows, str):
                try:
                    rows = json.loads(rows)
                except ValueError as exc:
                    return f"Error: rows is not valid JSON: {exc}"
            return json.dumps(self._svc("seed", str(args.get("table") or ""), json.dumps(rows or [])))
        if name == "env_sql":
            out = self._svc("sql", str(args.get("database") or "services"), json.dumps({"sql": str(args.get("sql") or "")}))
            return json.dumps(out, default=str)[:20000]
        out = self._bash(str(args.get("command") or ""), self.home, 120)
        return out.content
