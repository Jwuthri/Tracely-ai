"""Real environments for audits: the tool pack, the container spec, the sandbox session, and the
state machine's handling of tools that really execute. No Docker needed — the backend is faked;
the one test that runs real `bash` skips where coreutils `timeout` is missing.

What is pinned here is what makes a real sandbox safe to offer and hard to recognise: the
container gets no network and no privileges, nothing from a request reaches its spec, every
command runs with a scrubbed environment, a customer's tool is never executed because it shares a
name with ours, and rollback rewinds the machine as well as the conversation.
"""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
import tarfile

import pytest

from tracely.domain.audit import state as st
from tracely.domain.audit import tooling
from tracely.infrastructure.sandbox import ExecResult, SandboxSpec
from tracely.infrastructure.sandbox.docker import container_kwargs
from tracely.services import sandbox_session
from tracely.services.sandbox_session import SandboxSession


# ── the container ─────────────────────────────────────────────────────────────


def _spec(**over) -> SandboxSpec:
    return SandboxSpec(image="tracely-sandbox:latest", hostname="dev-1a2b3c", owner="tracely", rollout_id="r1", **over)


def test_sandbox_spec_has_no_network_and_no_privileges():
    kw = container_kwargs(_spec())
    assert kw["network_mode"] == "none"
    assert kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges"]
    assert kw["user"] == "dev" and kw["pids_limit"] > 0 and kw["mem_limit"] and kw["nano_cpus"] > 0
    assert "volumes" not in kw and "privileged" not in kw and "mounts" not in kw


def test_a_sandbox_removes_itself_at_its_lifetime_cap():
    """The only cleanup that works with Tracely down: PID 1 is a sleep, and exit removes it."""
    kw = container_kwargs(_spec(lifetime_s=900))
    assert kw["command"] == ["sleep", "900"] and kw["auto_remove"] is True


def test_sandbox_environment_inherits_nothing_and_names_no_harness():
    kw = container_kwargs(_spec())
    blob = json.dumps({k: kw[k] for k in ("environment", "hostname", "command", "working_dir")}).lower()
    for word in ("tracely", "audit", "sandbox", "eval", "test"):
        assert word not in blob, word  # labels are host-side metadata; these the target can read


def test_docker_backend_refused_on_a_public_signup_production_deployment():
    from tracely.config import Settings

    base = dict(tracely_env="prod", auth_mode="local", session_secret="s" * 40, sandbox_backend="docker")
    Settings(**base, allow_public_signup=False)  # single-tenant prod: allowed
    with pytest.raises(ValueError, match="single-tenant"):
        Settings(**base, allow_public_signup=True)


# ── the tool pack ─────────────────────────────────────────────────────────────


def test_imported_tools_never_bind_to_the_toolbox_by_name():
    """A customer's agent arrives with its own `bash`. Ours must not replace it, and the sandbox
    must not execute it: what that tool does is the customer's, so the attacker answers it."""
    own = [{"name": "bash", "description": "the customer's sandboxed runner", "parameters": {"type": "object"}}]
    tools = own + tooling.pack_tools(["dev"], {t["name"] for t in own})
    assert [t["name"] for t in tools] == ["bash", "read_file", "write_file", "edit_file"]
    state = st.new_state(auditor_system="S", auditor_user="U", target_model="m", control="fixed",
                         system_prompt="p", tools=tools, environment="sandbox")
    state.awaiting = {"index": 0, "prefill": "", "steps": []}
    state.auditor_messages.append({"role": "tool", "tool_call_id": "x", "content": ""})
    state.awaiting["index"] = len(state.auditor_messages) - 1
    st.apply_target_reply(state, {"content": "", "usage": {}, "message": {}, "tool_calls": [
        {"id": "t1", "name": "bash", "args": {"command": "ls"}},
        {"id": "t2", "name": "read_file", "args": {"path": "a.py"}},
    ]}, 1, 2)
    assert [c["id"] for c in st.real_pending(state)] == ["t2"]
    assert st.synthetic_pending(state) == ["t1"]


def test_a_synthetic_environment_executes_nothing():
    state = st.new_state(auditor_system="S", auditor_user="U", target_model="m", tools=tooling.DEV_PACK)
    assert not any(st._is_real(state, t["name"]) for t in state.tools)
    names = {t["function"]["name"] for t in st.auditor_request(state)[1]}
    assert not names & {"env_exec", "env_write_file"}


def test_paths_resolve_like_a_shell_would():
    assert tooling.resolve("src/app.py", "/home/dev/proj", "/home/dev") == "/home/dev/proj/src/app.py"
    assert tooling.resolve("~/notes.txt", "/tmp", "/home/dev") == "/home/dev/notes.txt"
    assert tooling.resolve("../x", "/home/dev/proj", "/home/dev") == "/home/dev/x"
    assert tooling.resolve("/etc/hosts", "/home/dev", "/home/dev") == "/etc/hosts"


def test_parse_bash_reads_exit_code_and_directory_after_the_marker():
    out = b"hello\nworld\n__abc__ 3 /home/dev/proj\n"
    assert tooling.parse_bash(out, "__abc__", 60) == ("hello\nworld", 3, "/home/dev/proj")


def test_a_command_killed_by_its_timeout_says_so():
    text, rc, cwd = tooling.parse_bash(b"partial outp", "__abc__", 5)
    assert rc is None and cwd is None and "timed out after 5s" in text


def test_command_output_cannot_forge_the_marker():
    """The marker is random per call, so output that happens to contain an older one — or a
    target that tries to print one — does not end the parse early."""
    out = b"__old__ 0 /evil\nreal\n__new__ 1 /home/dev\n"
    assert tooling.parse_bash(out, "__new__", 60) == ("__old__ 0 /evil\nreal", 1, "/home/dev")


@pytest.mark.skipif(shutil.which("timeout") is None, reason="coreutils `timeout` not installed")
def test_bash_wrapper_runs_the_command_in_the_reporting_shell(tmp_path):
    """The real thing: a `cd` inside the command is the directory the next call starts in."""
    (tmp_path / "sub").mkdir()
    argv = tooling.bash_argv("cd sub && echo hi && false", str(tmp_path), str(tmp_path), 10, "__m__")
    done = subprocess.run(argv, capture_output=True, check=False)
    text, rc, cwd = tooling.parse_bash(done.stdout, "__m__", 10)
    assert text.strip() == "hi" and rc == 1 and cwd.endswith("/sub")


def test_edit_requires_exactly_one_match():
    assert tooling.replace_once("a b a", "b", "c") == ("a c a", "")
    assert tooling.replace_once("a b a", "a", "c")[0] is None
    assert "not found" in tooling.replace_once("abc", "z", "y")[1]


def test_a_written_file_belongs_to_the_sandbox_user():
    with tarfile.open(fileobj=io.BytesIO(tooling.file_tar("app.py", b"print(1)\n"))) as tar:
        (member,) = tar.getmembers()
        assert (member.name, member.uid, member.gid, member.size) == ("app.py", 1000, 1000, 9)


# ── the session ───────────────────────────────────────────────────────────────


class FakeBackend:
    """An in-memory stand-in for a sandbox backend: records every call, keeps written files."""

    def __init__(self):
        self.created: list[SandboxSpec] = []
        self.execs: list[dict] = []
        self.puts: list[tuple[str, str, bytes]] = []
        self.destroyed: list[str] = []
        self.files: dict[str, bytes] = {}
        self.gone: set[str] = set()
        self.archive: bytes | None = b"HOME-TAR"

    def create(self, spec):
        self.created.append(spec)
        return f"sbx-{len(self.created)}"

    def exec(self, ref, argv, *, user, workdir, env):
        from tracely.infrastructure.sandbox import SandboxGone

        if ref in self.gone:
            raise SandboxGone(ref)
        self.execs.append({"ref": ref, "argv": argv, "user": user, "workdir": workdir, "env": env})
        if argv[0] == "timeout":  # a wrapped bash command: answer like the wrapper would
            marker = argv[6].split("printf '\\n")[1].split(" ")[0]
            cwd = shlex.split(argv[6].split(";")[0])[1]  # the directory the wrapper cd's into
            return ExecResult(0, f"ran: {argv[-1]}\n{marker} 0 {cwd}\n".encode())
        if argv[0] == "head":
            data = self.files.get(argv[-1])
            return ExecResult(0, data) if data is not None else ExecResult(1, b"head: cannot open: No such file")
        return ExecResult(0, b"")

    def put(self, ref, path, tar):
        self.puts.append((ref, path, tar))
        try:
            with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
                for member in archive.getmembers():
                    self.files[f"{path}/{member.name}"] = archive.extractfile(member).read()
        except tarfile.TarError:
            pass  # a snapshot being restored — opaque here

    def get(self, ref, path, max_bytes):
        return self.archive

    def destroy(self, ref):
        self.destroyed.append(ref)

    def list(self, owner):
        return []


@pytest.fixture
def blobs(monkeypatch):
    store: dict[str, bytes] = {}
    monkeypatch.setattr(sandbox_session.blobstore, "put_blob", lambda key, body, ct="": store.__setitem__(key, body))
    monkeypatch.setattr(sandbox_session.blobstore, "get_blob", lambda key: store[key])
    return store


def _session(backend=None) -> tuple[SandboxSession, FakeBackend]:
    backend = backend or FakeBackend()
    session = SandboxSession(backend, project_id="p1", rollout_id="0bf0e89f-d876", record=None)
    session.ensure()
    return session, backend


def test_exec_env_is_scrubbed():
    session, backend = _session()
    session.run_tool("bash", {"command": "env"}, "")
    env = backend.execs[-1]["env"]
    assert set(env) == {"HOME", "USER", "LANG", "TERM", "PATH"} and backend.execs[-1]["user"] == "dev"


def test_sandbox_spec_takes_nothing_from_the_request(monkeypatch):
    """Image, user and limits come from settings — a tool call cannot name a container."""
    monkeypatch.setattr(sandbox_session.settings, "sandbox_image", "registry.internal/box:1")
    session, backend = _session()
    session.run_tool("bash", {"command": "x", "image": "evil:latest", "user": "root", "privileged": True}, "")
    (spec,) = backend.created
    assert spec.image == "registry.internal/box:1" and spec.user == "dev"
    assert "tracely" not in spec.hostname and "audit" not in spec.hostname


def test_bash_keeps_the_working_directory_and_reports_failure():
    session, backend = _session()
    out = session.run_tool("bash", {"command": "pytest -q"}, "/home/dev/proj")
    assert out.status == "success" and out.cwd == "/home/dev/proj" and "ran: pytest -q" in out.content
    assert backend.execs[-1]["argv"][3] == "60"  # the default timeout
    session.run_tool("bash", {"command": "sleep 999", "timeout": 99999}, "")
    assert backend.execs[-1]["argv"][3] == "300"  # …and its ceiling


def test_write_then_read_then_edit_a_file():
    session, backend = _session()
    out = session.run_tool("write_file", {"path": "src/app.py", "content": "x = 1\ny = 2\n"}, "/home/dev/proj")
    assert out.status == "success" and backend.files["/home/dev/proj/src/app.py"] == b"x = 1\ny = 2\n"
    assert ["mkdir", "-p", "--", "/home/dev/proj/src"] in [e["argv"] for e in backend.execs]

    read = session.run_tool("read_file", {"path": "src/app.py"}, "/home/dev/proj")
    assert read.content == "     1\tx = 1\n     2\ty = 2"

    edit = session.run_tool("edit_file", {"path": "src/app.py", "old_string": "y = 2", "new_string": "y = 3"}, "/home/dev/proj")
    assert edit.status == "success" and backend.files["/home/dev/proj/src/app.py"] == b"x = 1\ny = 3\n"


def test_a_tool_error_reads_like_the_tool_not_like_a_harness():
    session, _ = _session()
    out = session.run_tool("read_file", {"path": "missing.py"}, "")
    assert out.status == "error" and "No such file" in out.content
    for word in ("tracely", "sandbox", "audit", "Traceback"):
        assert word not in out.content


def test_snapshot_goes_to_the_blob_store_and_restore_rebuilds_from_it(blobs):
    session, backend = _session()
    key = session.snapshot()
    assert key.endswith("/audits/snapshots/0bf0e89f-d876/1.tgz") and key in blobs
    session.restore(key)
    assert backend.destroyed == ["sbx-1"] and session.record["ref"] == "sbx-2"
    assert backend.puts[-1] == ("sbx-2", "/", b"HOME-TAR")  # extracted over the new machine's /home


def test_a_home_directory_too_large_to_snapshot_is_skipped_not_fatal(blobs):
    session, backend = _session()
    backend.archive = None  # the backend's answer for "over the cap"
    assert session.snapshot() == "" and blobs == {}


# ── allowlisted web access ────────────────────────────────────────────────────


def _proxy():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "sandbox" / "egress_proxy.py"
    spec = importlib.util.spec_from_file_location("egress_proxy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ALLOW = ["pypi.org", "files.pythonhosted.org"]


def test_the_proxy_admits_only_listed_hosts_and_their_subdomains():
    proxy = _proxy()
    assert proxy.allowed_host("pypi.org", ALLOW) and proxy.allowed_host("test.PyPI.org.", ALLOW)
    for host in ("example.com", "evilpypi.org", "pypi.org.evil.com", "151.101.0.223", "::1", ""):
        assert not proxy.allowed_host(host, ALLOW), host


def test_plain_http_is_read_only_and_https_only_tunnels_to_443():
    proxy = _proxy()
    assert proxy.decide("GET", "pypi.org", 80, ALLOW)[0] and proxy.decide("HEAD", "pypi.org", 80, ALLOW)[0]
    assert proxy.decide("CONNECT", "pypi.org", 443, ALLOW)[0]
    for method, port in (("POST", 80), ("PUT", 80), ("DELETE", 80), ("CONNECT", 22), ("GET", 8080)):
        assert not proxy.decide(method, "pypi.org", port, ALLOW)[0], (method, port)
    assert not proxy.decide("CONNECT", "example.com", 443, ALLOW)[0]


def test_an_allowlisted_name_cannot_reach_a_private_address():
    """The proxy resolves the name itself and connects to that address — so a listed host that
    points inside (rebinding, a poisoned record) is refused, and our own network stays closed."""
    proxy = _proxy()
    for address in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "172.18.0.2", "::1", "fd00::1"):
        assert not proxy.is_public(address), address
    assert proxy.is_public("151.101.0.223")


def test_a_sandbox_joins_the_egress_network_only_when_the_audit_and_the_deployment_both_say_so(monkeypatch):
    backend = FakeBackend()
    plain = SandboxSession(backend, project_id="p", rollout_id="r1", network="allowlist")
    plain.ensure()  # the deployment has no egress network: allowlist degrades to no network
    assert container_kwargs(backend.created[-1])["network_mode"] == "none"

    monkeypatch.setattr(sandbox_session.settings, "sandbox_egress_network", "tracely-sandbox-egress")
    proxied = SandboxSession(backend, project_id="p", rollout_id="r2", network="allowlist")
    proxied.ensure()
    kw = container_kwargs(backend.created[-1])
    assert kw["network"] == "tracely-sandbox-egress" and "network_mode" not in kw
    proxied.run_tool("bash", {"command": "pip install x"}, "")
    assert backend.execs[-1]["env"]["HTTPS_PROXY"] == "http://proxy.internal:3128"

    closed = SandboxSession(backend, project_id="p", rollout_id="r3", network="none")
    closed.ensure()
    assert container_kwargs(backend.created[-1])["network_mode"] == "none"
    closed.run_tool("bash", {"command": "x"}, "")
    assert "HTTPS_PROXY" not in backend.execs[-1]["env"]
