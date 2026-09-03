"""`tracely` CLI — the CI/CD gate.

Turn an agent's test suite into a real pull-request check. Three ways in:

    tracely gate --agent planner       # gate on traces CI already emitted (tracely.env=ci)
    tracely replay --agent planner …   # re-run promoted cases through your code, then gate
    tracely simulate --agent planner   # drive the agent's scenarios against its HTTP endpoint

`simulate` needs no agent code in CI at all — Tracely calls the endpoint you registered and
drives each scenario as a multi-turn conversation, so this works for a TypeScript or Go service
just as well as a Python one.

Exits 0 on PASS, 1 on FAIL, 2 on error — so it blocks a merge on its own. Inside a
GitHub Actions run (or with --github) it also posts a commit status + a PR comment,
linking to a PUBLIC, login-free page for that gate run so every reviewer can read the
verdict (falling back to the authed gate page if a link can't be minted). Stdlib only,
so it installs with the SDK and runs anywhere CI does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

from tracely_sdk.export import download_export

MARKER = "<!-- tracely-gate -->"
STATUS_CONTEXT = "tracely/regression-gate"
ICON = {"PASS": "✓", "FAIL": "✗", "ERROR": "✗", "SKIP": "–", "NO_COVERAGE": "⚠", "UNGRADED": "⚠"}
EMOJI = {"PASS": "✅", "FAIL": "❌", "ERROR": "❌", "SKIP": "⏭️", "NO_COVERAGE": "⚠️", "UNGRADED": "⚠️"}


# ── Tracely API ──────────────────────────────────────────────────────────────


# Per-request socket timeout. Without one, a single hung connection ignores the polling
# budget entirely (that budget is only checked *between* requests) and the CI job hangs
# forever with no output and no exit code.
_HTTP_TIMEOUT_S = 60


def _get_json(url: str, key: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    return json.load(urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S))


def _post_json(url: str, key: str, body: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S))


def trigger_gate(
    api: str,
    key: str,
    agent: str,
    env: str,
    git_ref: str,
    pr: int | None,
    candidates: dict[str, str] | None = None,
) -> dict:
    body: dict = {"agent": agent, "env": env, "git_ref": git_ref, "pr_number": pr}
    if candidates:
        body["candidates"] = candidates  # explicit case_id -> trace_id pairing from replay
    return _post_json(f"{api.rstrip('/')}/api/gate", key, body)


def case_reason(detail: dict) -> str:
    """A short, human reason for a non-PASS case from the gate's detail payload.

    Covers both kinds of case: a replayed regression case (tool sequence / errors / quality) and
    an emulated conversation (transport error, failed expectations, failed evaluators).
    """
    d = detail or {}
    bits: list[str] = []
    # ── emulated conversation ────────────────────────────────────────────────
    if d.get("error"):  # the endpoint never answered — that IS the failure
        bits.append(str(d["error"]))
    bits += [f"✗ {f}" for f in (d.get("failed_expectations") or [])]
    bits += [f"✗ {f}" for f in (d.get("failed_scores") or [])]
    # ── replayed regression case ─────────────────────────────────────────────
    if d.get("missing_tools"):
        bits.append("missing tools: " + ", ".join(d["missing_tools"]))
    if d.get("run_errors"):  # run-outcome assertion: the agent itself failed
        bits.append("run failed: " + ", ".join(d["run_errors"]))
    elif d.get("allow_tool_errors") and d.get("tool_errors"):  # tolerated (the agent handled it)
        bits.append("tool errored (handled): " + ", ".join(d["tool_errors"]))
    elif d.get("erroring_steps"):
        bits.append("errors: " + ", ".join(d["erroring_steps"]))
    if not d.get("tools_ok", True) and not d.get("missing_tools"):
        bits.append(f"tool sequence mismatch (mode={d.get('match_mode', '')})")
    if d.get("quality_pass") is False:  # judge-in-the-gate: the replayed answer is still bad
        q = "answer quality below bar"
        if d.get("quality_reason"):
            q += f": {d['quality_reason']}"
        bits.append(q)
    if d.get("reason"):  # SKIP carries a plain reason
        bits.append(str(d["reason"]))
    return "; ".join(bits)


def ungraded_note(verdict: str, detail: dict) -> str:
    """UNGRADED has no failing score to point at — it means nothing graded the conversation at
    all, which counts against the pass rate. Say so explicitly or the row reads as unexplained."""
    if verdict != "UNGRADED":
        return ""
    turns = (detail or {}).get("turns")
    return (
        f"ran {turns} turn(s) but nothing scored it — no evaluator matched, or the judge was "
        "unavailable. Counts against the pass rate, never as a pass."
        if turns
        else "nothing scored this conversation"
    )


# ── console + markdown rendering ─────────────────────────────────────────────


def render_console(data: dict, sha: str) -> None:
    print(f"\nTracely gate · agent={data.get('agent')} · env={data.get('env')} · {sha[:8]}")
    print(f"  {data['passed']} passed · {data['failed']} failed · {data['skipped']} skipped\n")
    for c in data.get("cases", []):
        reason = case_reason(c.get("detail") or {}) or ungraded_note(
            c["verdict"], c.get("detail") or {}
        )
        extra = f"  ({reason})" if reason else ""
        print(f"  {ICON.get(c['verdict'], '?')} {c['verdict']:<11} {c['title']}{extra}")
    for w in data.get("warnings") or []:
        print(f"  ⚠️  {w}")
    if data["status"] == "NO_COVERAGE":
        print(
            f"\n  ⚠ NO COVERAGE — 0 of {data.get('total', 0)} case(s) were actually graded. "
            "Either CI emitted no trace matching a promoted case, or every conversation ran "
            "ungraded. Treated as a failure (a gate that tests nothing must not pass)."
        )
    print(f"\n  Result: {data['status']}\n")


_HEAD = {"FAIL": "🔴", "ERROR": "🔴", "PASS": "🟢", "NO_COVERAGE": "🟠"}

# Worst-wins across agents. Anything that isn't PASS blocks, but the headline should name the most
# alarming thing that happened, not the first one alphabetically.
_RANK = ("PASS", "NO_COVERAGE", "FAIL", "ERROR")


def worst_status(results: list[dict]) -> str:
    return max(
        (r["status"] for r in results),
        key=lambda s: _RANK.index(s) if s in _RANK else len(_RANK),
        default="PASS",
    )


def gate_link(data: dict, web_url: str) -> str:
    """Where the PR comment points. The PUBLIC share link when one was minted, the authed gate page
    otherwise — a reviewer without a Tracely login is the whole point, so the login-walled URL is
    the fallback, not the default."""
    if data.get("share_url"):
        return data["share_url"]
    if web_url and data.get("id"):
        return f"{web_url.rstrip('/')}/gates/{data['id']}"
    return f"{web_url.rstrip('/')}/gates" if web_url else ""


def mint_share_url(api: str, key: str, web_url: str, gate_id: str) -> str:
    """A login-free URL for one gate run, or "" if we can't get one.

    Never raises and never blocks the check: an older backend (no `kind` on /api/share), a key
    without permission, or an unreachable API all mean "post the authed link instead". The gate's
    own verdict and exit code do not depend on this."""
    if not (api and key and web_url and gate_id):
        return ""
    try:
        res = _post_json(f"{api.rstrip('/')}/api/share", key, {"kind": "gate", "id": gate_id})
        token = (res or {}).get("token")
        return f"{web_url.rstrip('/')}/share/{token}" if token else ""
    except Exception as e:  # noqa: BLE001 — a link is a nicety; the check must still post
        print(f"note: could not mint a public share link ({e}); linking to the authed gate page")
        return ""


def render_markdown(data: dict, web_url: str, sha: str) -> str:
    status = data["status"]
    head = _HEAD.get(status, "⚪")
    sha_txt = f"`{sha[:7]}`" if sha else ""
    lines = [
        MARKER,
        f"### {head} Tracely regression gate — **{status}**",
        "",
        f"`{data.get('agent')}` · {data['passed']} passed · {data['failed']} failed · "
        f"{data['skipped']} skipped · env `{data.get('env')}` · {sha_txt}",
        "",
        "| | Case | Verdict | Detail |",
        "|---|---|---|---|",
    ]
    # `/sessions/...` is behind the login wall. Linking every case there is right when the only
    # readers are teammates with accounts, and wrong the moment the comment carries a PUBLIC
    # verdict link: a stranger follows the case title expecting detail and lands on a sign-in
    # form — the same wall the public page exists to remove, one level down. So the per-case link
    # is emitted only when there is no public link to be a stranger's entry point instead.
    # Making the CONVERSATION itself public is not the alternative: that payload carries the full
    # prompt/response text (see `share.py`), which a CI-minted link must never publish.
    link_cases = bool(web_url) and not data.get("share_url")
    for c in data.get("cases", []):
        detail = c.get("detail") or {}
        reason = (case_reason(detail) or ungraded_note(c["verdict"], detail)).replace("|", "\\|")
        title = c["title"]
        if link_cases and c.get("scenario_id") and c.get("candidate_trace_id"):
            thread = f"{web_url.rstrip('/')}/sessions/{c['candidate_trace_id']}"
            title = f"[{title}]({thread})"
        lines.append(
            f"| {EMOJI.get(c['verdict'], '❔')} | {title} | {c['verdict']} | {reason} |"
        )
    lines.append("")
    warnings = data.get("warnings") or []
    if warnings:
        lines.append("**⚠️ Soft warnings** (non-blocking):")
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    if status == "FAIL":
        lines.append(
            "> These regression tests were promoted from **real production failures**. "
            "A FAIL means this change reintroduces — or fails to fix — a known failure."
        )
        lines.append("")
    if status == "NO_COVERAGE":
        lines.append(
            f"> ⚠️ **No coverage.** This run graded **0 of {data.get('total', 0)}** "
            "case(s) — CI emitted no trace matching them (a misconfigured replay step, "
            "a renamed agent, or an input-digest mismatch). A gate that tests nothing is **not** a "
            "pass — fix the CI step so the suite actually runs."
        )
        lines.append("")
    link = gate_link(data, web_url)
    if link:
        lines.append(f"[View full verdict on Tracely →]({link})")
    return "\n".join(lines)


def render_markdown_all(results: list[dict], web_url: str, sha: str) -> str:
    """One comment covering every agent gated in this run.

    It has to be one: GitHub keys the commit status by context and our comment by the hidden
    marker, so posting per agent would leave only the last one visible — a red agent could be
    silently overwritten by a green one that finished after it.
    """
    if len(results) == 1:
        return render_markdown(results[0], web_url, sha)
    worst = worst_status(results)
    lines = [
        MARKER,
        f"### {_HEAD.get(worst, '⚪')} Tracely gate — **{worst}** · {len(results)} agents",
        "",
        "| | Agent | Result | Cases |",
        "|---|---|---|---|",
    ]
    for d in results:
        counts = f"{d['passed']} passed · {d['failed']} failed · {d['skipped']} skipped"
        lines.append(
            f"| {EMOJI.get(d['status'], '❔')} | `{d.get('agent')}` | {d['status']} | {counts} |"
        )
    for d in results:
        # ponytail: reuse the single-agent renderer and drop its marker rather than parameterising
        # the heading level — one line instead of splitting a 50-line function in two.
        body = render_markdown(d, web_url, sha).replace(MARKER, "").strip()
        lines += [
            "",
            "<details>",
            f"<summary>{EMOJI.get(d['status'], '❔')} <b>{d.get('agent')}</b> — "
            f"{d['status']}</summary>",
            "",
            body,
            "",
            "</details>",
        ]
    return "\n".join(lines)


# ── GitHub ───────────────────────────────────────────────────────────────────


def gh_context() -> tuple[str, str, int | None]:
    """(repo, head_sha, pr_number) resolved from the GitHub Actions environment."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    pr: int | None = None
    ev_path = os.environ.get("GITHUB_EVENT_PATH")
    if ev_path and os.path.exists(ev_path):
        try:
            with open(ev_path) as f:
                ev = json.load(f)
            prinfo = ev.get("pull_request") or {}
            if prinfo.get("number"):
                pr = int(prinfo["number"])
            head = (prinfo.get("head") or {}).get("sha")
            if head:
                sha = head  # post to the PR head commit, not the merge commit
        except Exception:
            pass
    if pr is None:
        m = re.match(r"refs/pull/(\d+)/", os.environ.get("GITHUB_REF", ""))
        if m:
            pr = int(m.group(1))
    return repo, sha, pr


class GitHub:
    def __init__(self, token: str, dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        if self.dry_run:
            print(f"[dry-run] {method} {url}")
            if body is not None:
                print(json.dumps(body, indent=2))
            return {"id": 0, "html_url": url}
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r) if r.length != 0 else {}
        except urllib.error.HTTPError as e:
            print(f"github {method} {path} -> {e.code}: {e.read().decode()[:300]}")
            return None

    def commit_status(self, repo: str, sha: str, state: str, description: str, target_url: str):
        self._call(
            "POST",
            f"/repos/{repo}/statuses/{sha}",
            {
                "state": state,  # success | failure | error | pending
                "context": STATUS_CONTEXT,
                "description": description[:140],
                **({"target_url": target_url} if target_url else {}),
            },
        )

    def upsert_comment(self, repo: str, pr: int, body: str):
        # update our previous comment in place (keyed by the hidden marker) instead of spamming
        existing = (
            []
            if self.dry_run
            else (self._call("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100") or [])
        )
        prior = next(
            (c for c in existing if isinstance(c, dict) and MARKER in (c.get("body") or "")), None
        )
        if prior:
            self._call("PATCH", f"/repos/{repo}/issues/comments/{prior['id']}", {"body": body})
        else:
            self._call("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(markdown + "\n")


# ── command ──────────────────────────────────────────────────────────────────


def post_pr_check(
    args: argparse.Namespace,
    results: list[dict],
    web_url: str,
    repo: str,
    sha: str,
    pr: int | None,
    api: str = "",
    key: str = "",
) -> None:
    """Post the gate result to GitHub (commit status + PR comment) when running in/for Actions.

    Takes every gate in the run, not one: `simulate --all` gates N agents and they share a single
    commit status and a single comment (see `render_markdown_all`).
    """
    token = args.token or os.environ.get("GITHUB_TOKEN", "")
    want_github = args.github or (os.environ.get("GITHUB_ACTIONS") == "true" and token)
    if not want_github or args.no_github:
        return
    if not token:
        print("note: --github requested but no GITHUB_TOKEN; skipping PR check")
        return
    if not repo:
        print("note: not in a GitHub repo context (no GITHUB_REPOSITORY); skipping PR check")
        return
    gh = GitHub(token, dry_run=args.dry_run)
    # Mint the public link now, not at gate time: this is the only place we know the result is
    # actually going to GitHub, and the link is stamped onto each result so both renderers see it.
    for d in results:
        if d.get("id"):
            d["share_url"] = mint_share_url(api, key, web_url, d["id"])
    # NO_COVERAGE is a blocking non-PASS (the gate exercised nothing) → a failing check, not a
    # transient "error". Exit code is already non-zero for any non-PASS (see cmd_gate/cmd_replay).
    state = {"PASS": "success", "FAIL": "failure", "NO_COVERAGE": "failure"}.get(
        worst_status(results), "error"
    )
    totals = {k: sum(r[k] for r in results) for k in ("passed", "failed", "skipped")}
    desc = f"{totals['passed']} passed · {totals['failed']} failed · {totals['skipped']} skipped"
    if len(results) > 1:
        desc += f" · {len(results)} agents"
    # One agent links straight to its run (publicly, when we could mint a link); several link to
    # the list, since there is no single run to open — and no public page for a set of runs.
    target = gate_link(results[0], web_url) if len(results) == 1 else ""
    if not target and web_url:
        target = f"{web_url.rstrip('/')}/gates"
    if sha:
        gh.commit_status(repo, sha, state, desc, target)
    if pr:
        gh.upsert_comment(repo, pr, render_markdown_all(results, web_url, sha))
    print(
        f"posted gate check to {repo}"
        + (f" PR #{pr}" if pr else "")
        + (" (dry-run)" if args.dry_run else "")
    )


# The seeded local key. Handy as a default against localhost; never valid anywhere else — a
# deployment refuses to boot with it (`TRACELY_ENV=prod`), so reaching a real API with it always
# means "TRACELY_KEY was never set".
DEV_KEY = "tracely_dev_key"


def _auth_hint(api: str, key: str) -> str:
    """Why a 401 happened, in the two ways it actually goes wrong.

    Both arrive as the same `invalid ingest key`, and the difference is the whole fix: one is a
    secret that was never set, the other one that no longer matches. Without this the CI log reads
    like the key is bad when the common cause is that CI has no key at all."""
    if key == DEV_KEY:
        return (
            f"\n  hint: TRACELY_KEY is unset, so the local dev key was sent to {api}."
            "\n        In GitHub Actions that means the secret is missing or empty —"
            "\n        set it from Settings → API keys in the workspace you want to gate."
        )
    return (
        f"\n  hint: TRACELY_KEY is set but no key on {api} matches it — it was most likely"
        "\n        rotated, or it belongs to a workspace that no longer exists."
        "\n        Copy the current key from Settings → API keys."
    )


def _conn(args: argparse.Namespace) -> tuple[str, str, str, str]:
    # An unset GitHub secret still renders as an env var set to "" — treat empty as absent, or the
    # default never applies and the api base becomes "" (urllib: "unknown url type: '/api/…'").
    def env(name: str, default: str = "") -> str:
        return (os.environ.get(name) or "").strip() or default

    api = (args.api or "").strip() or env("TRACELY_API", "http://localhost:8000")
    key = (args.key or "").strip() or env("TRACELY_KEY", DEV_KEY)
    web_url = (args.web_url or "").strip() or env("TRACELY_WEB_URL")
    agent = (args.agent or "").strip() or env("TRACELY_AGENT")
    return api, key, web_url, agent


def cmd_gate(args: argparse.Namespace) -> int:
    api, key, web_url, agent = _conn(args)
    if not agent:
        print("error: --agent (or TRACELY_AGENT) is required")
        return 2

    repo, sha, pr = gh_context()
    sha = args.sha or sha
    if args.pr is not None:
        pr = args.pr
    git_ref = sha or os.environ.get("GIT_REF", "")

    try:
        data = trigger_gate(api, key, agent, args.env, git_ref, pr)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        print(f"gate error: {e.code} {detail}{_auth_hint(api, key) if e.code == 401 else ''}")
        return 2
    except urllib.error.URLError as e:
        print(f"gate error: cannot reach Tracely at {api}: {e.reason}")
        return 2

    render_console(data, sha)
    write_step_summary(render_markdown(data, web_url, sha))
    post_pr_check(args, [data], web_url, repo, sha, pr, api, key)
    return 0 if data["status"] == "PASS" else 1


def start_simulation(
    api: str, key: str, agent: str, env: str, git_ref: str, pr: int | None, min_pass_rate=None
) -> dict:
    body: dict = {"agent": agent, "env": env, "git_ref": git_ref, "pr_number": pr}
    if min_pass_rate is not None:
        body["min_pass_rate"] = min_pass_rate
    return _post_json(f"{api.rstrip('/')}/api/gate/simulate", key, body)


def poll_gate(api: str, key: str, gate_id: str, timeout: int, quiet: bool = False) -> dict:
    """Block until the gate finishes, or raise TimeoutError.

    A simulated gate drives real conversations against the customer's agent and then waits on the
    eval pipeline, so it is minutes of work — the API hands back a RUNNING row immediately and CI
    polls `finished_at`. Timing out is NOT treated as a pass: `cmd_simulate` exits non-zero, since
    a merge-blocker that gave up must never read as green.
    """
    import time

    url = f"{api.rstrip('/')}/api/gates/{gate_id}"
    deadline = time.time() + timeout
    waited = 0
    while time.time() < deadline:
        data = _get_json(url, key)
        if data.get("finished_at"):
            return data
        if not quiet and waited and waited % 30 == 0:
            print(f"  … still running ({waited}s)")
        time.sleep(5)
        waited += 5
    raise TimeoutError(f"gate {gate_id} did not finish within {timeout}s")


def discover_agents(api: str, key: str) -> list[str]:
    """Every agent with at least one ENABLED scenario — what `--all` gates.

    Derived from the scenario list rather than the agent list on purpose: an agent with no enabled
    scenario has nothing to simulate, and gating it would report NO_COVERAGE and block the PR for
    a suite that was never written.
    """
    out: list[str] = []
    for sc in _get_json(f"{api.rstrip('/')}/api/scenarios", key):
        name = sc.get("agent") or sc.get("agent_id")
        if sc.get("enabled") and name and name not in out:
            out.append(name)
    return out


def agent_list(args: argparse.Namespace) -> list[str]:
    """Agents named on the command line: positional, repeated `--agent`, or comma-separated."""
    raw = list(getattr(args, "agents", None) or [])
    if getattr(args, "agent", None):
        raw.append(args.agent)
    if not raw:
        raw = [os.environ.get("TRACELY_AGENT", "")]
    out: list[str] = []
    for chunk in raw:
        for name in str(chunk).split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def _errored(agent: str, message: str) -> dict:
    """A gate we never got a verdict for, shaped like one so it renders and blocks like one."""
    return {
        "id": "", "agent": agent, "env": "", "status": "ERROR",
        "passed": 0, "failed": 0, "skipped": 0, "total": 0,
        "cases": [], "warnings": [message],
        # Distinguishes "we never got an answer" (exit 2) from "the answer was no" (exit 1).
        "unreachable": True,
    }


def cmd_simulate(args: argparse.Namespace) -> int:
    import time

    api, key, web_url, _ = _conn(args)
    agents = agent_list(args)
    if getattr(args, "all_agents", False):
        # `--all` always wins. CI envs routinely export TRACELY_AGENT, and letting that ambient
        # value silently shrink `--all` to one agent gates a fraction of the suite while exiting 0.
        if agents:
            print(f"note: --all overrides {', '.join(agents)} — gating every agent with an enabled scenario")
        try:
            agents = discover_agents(api, key)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"simulate error: cannot list scenarios at {api}: {e}")
            return 2
        if not agents:
            print("error: --all found no agent with an enabled scenario")
            return 2
        print(f"--all → {len(agents)} agent(s): {', '.join(agents)}")
    if not agents:
        print("error: name an agent (--agent, or TRACELY_AGENT), or pass --all")
        return 2

    repo, sha, pr = gh_context()
    sha = args.sha or sha
    if args.pr is not None:
        pr = args.pr

    # Start every gate before waiting on any of them. The server queues the work, so overlapping
    # the waits is the difference between one timeout and N of them end to end.
    started: list[tuple[str, str | None]] = []
    results: list[dict] = []
    for agent in agents:
        try:
            gate = start_simulation(api, key, agent, args.env, sha or "", pr, args.min_pass_rate)
            print(f"driving scenarios for {agent} (gate {gate['id'][:8]}…)")
            started.append((agent, gate["id"]))
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            results.append(
                _errored(agent, f"{detail}{_auth_hint(api, key)}" if e.code == 401 else f"{e.code} {detail}")
            )
        except urllib.error.URLError as e:
            results.append(_errored(agent, f"cannot reach Tracely at {api}: {e.reason}"))

    # One shared budget: `--timeout` bounds the whole command, not each agent, so N agents can't
    # multiply a 15-minute cap into an hour.
    deadline = time.time() + args.timeout
    for agent, gate_id in started:
        try:
            results.append(poll_gate(api, key, gate_id, int(max(0, deadline - time.time()))))
        except TimeoutError:
            results.append(_errored(agent, f"gate {gate_id[:8]} did not finish in {args.timeout}s"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            results.append(_errored(agent, f"could not read gate {gate_id[:8]}: {e}"))

    for data in results:
        render_console(data, sha)
    if len(results) > 1:
        worst = worst_status(results)
        passed = sum(r["status"] == "PASS" for r in results)
        print(f"  {passed}/{len(results)} agents passed · Result: {worst}\n")
    write_step_summary(render_markdown_all(results, web_url, sha))
    post_pr_check(args, results, web_url, repo, sha, pr, api, key)
    # 0 pass · 1 the gate said no · 2 we never got an answer (timeout, unreachable API, or the
    # server marked the run ERROR). All non-zero, so the merge blocks either way — the split just
    # says whose fault it was.
    if worst_status(results) == "PASS":
        return 0
    infra = any(r.get("unreachable") or r.get("status") == "ERROR" for r in results)
    return 2 if infra else 1


def _load_entrypoint(spec: str):
    """Import a 'module:function' entrypoint from the current working directory."""
    import importlib

    if ":" not in spec:
        raise SystemExit("--entrypoint must be 'module:function' (e.g. my_agent:run)")
    mod_name, fn_name = spec.split(":", 1)
    sys.path.insert(0, os.getcwd())
    return getattr(importlib.import_module(mod_name), fn_name)


def _wait_for_traces(api: str, key: str, trace_ids: list[str], timeout: int = 45) -> bool:
    """Poll until the emitted traces have been ingested into ClickHouse (or time out)."""
    import time

    deadline = time.time() + timeout
    pending = set(trace_ids)
    while pending and time.time() < deadline:
        for tid in list(pending):
            try:
                if _get_json(f"{api.rstrip('/')}/api/traces/{tid}", key).get("spans"):
                    pending.discard(tid)
            except Exception:
                pass
        if pending:
            time.sleep(2)
    if pending:
        print(
            f"warning: {len(pending)} replayed trace(s) not ingested in {timeout}s; gating anyway"
        )
    return not pending


def cmd_replay(args: argparse.Namespace) -> int:
    api, key, web_url, agent = _conn(args)
    if not agent:
        print("error: --agent (or TRACELY_AGENT) is required")
        return 2
    if not args.entrypoint and not args.cmd:
        print("error: provide --entrypoint module:func  or  --cmd '...'")
        return 2

    try:
        suite = _get_json(f"{api.rstrip('/')}/api/gate/suite?agent={agent}", key)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        print(f"replay error: {e.code} {detail}{_auth_hint(api, key) if e.code == 401 else ''}")
        return 2
    cases = suite.get("cases", [])
    if not cases:
        print(f"no promoted cases for '{agent}' — nothing to replay (promote a failure first)")
        return 0
    print(f"replaying {len(cases)} case(s) for {agent} (env={args.env})\n")

    pairings: dict[str, str] = {}
    if args.entrypoint:
        func = _load_entrypoint(args.entrypoint)
        import tracely_sdk as t  # lazy: only the replay path needs the tracing stack

        t.init(endpoint=api, api_key=key, service_name=agent, env=args.env)
        for c in cases:
            bundle = None if args.live else c.get("fixtures")
            with t.fixtures(bundle), t.agent(agent) as span:  # hermetic unless --live
                t.set_io(span, input=c["input"])
                try:
                    out = func(c["input"])
                except Exception as exc:  # a crashing agent is itself a failing replay
                    t.error(span, f"agent raised: {exc}")
                    out = f"<error: {exc}>"
                t.set_io(span, output=out if isinstance(out, str) else json.dumps(out, default=str))
                tid = format(span.get_span_context().trace_id, "032x")
            n_fx = len((bundle or {}).get("tools") or {}) + len((bundle or {}).get("llm") or {})
            pairings[c["id"]] = tid
            tag = f"  [{n_fx} fixtures]" if n_fx else "  [live]"
            print(f"  · {c['title']}  ->  {tid[:12]}…{tag}")
        t.flush()
        _wait_for_traces(api, key, list(pairings.values()))
    else:
        import subprocess
        import time

        for c in cases:
            env = {
                **os.environ,
                "TRACELY_INPUT": c["input"],
                "TRACELY_API": api,
                "TRACELY_KEY": key,
                "TRACELY_ENV": args.env,
            }
            subprocess.run(args.cmd, shell=True, env=env, check=False)
            print(f"  · ran cmd for {c['title']}")
        time.sleep(8)  # external process emits its own trace; give ingestion a moment

    repo, sha, pr = gh_context()
    sha = args.sha or sha
    if args.pr is not None:
        pr = args.pr
    # explicit pairing for the entrypoint path; digest matching for the --cmd path
    data = trigger_gate(api, key, agent, args.env, sha or "", pr, candidates=pairings or None)

    render_console(data, sha)
    write_step_summary(render_markdown(data, web_url, sha))
    post_pr_check(args, [data], web_url, repo, sha, pr, api, key)
    return 0 if data["status"] == "PASS" else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Dump the workspace's conversations as NDJSON — one line per conversation.

    Defaults to stdout so it pipes into jq; `--out` writes a file and reports the size on stderr,
    keeping stdout pure NDJSON either way.
    """
    api, key, _, _ = _conn(args)
    written = download_export(
        args.out or sys.stdout.buffer,
        api=api,
        key=key,
        limit=args.limit,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
        evals=args.evals,
        meta=args.meta,
    )
    if args.out:
        print(f"wrote {args.out} ({written} bytes)", file=sys.stderr)
    return 0


def _add_common_gate_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--env", default=os.environ.get("TRACELY_GATE_ENV", "ci"))
    sp.add_argument("--api", help="Tracely API base (TRACELY_API)")
    sp.add_argument("--key", help="Tracely ingest key (TRACELY_KEY)")
    sp.add_argument("--web-url", help="Tracely web base for links (TRACELY_WEB_URL)")
    sp.add_argument("--pr", type=int, help="PR number (else inferred from the Actions event)")
    sp.add_argument("--sha", help="commit SHA (else inferred)")
    sp.add_argument("--github", action="store_true", help="post a commit status + PR comment")
    sp.add_argument(
        "--no-github", action="store_true", help="never touch GitHub even inside Actions"
    )
    sp.add_argument("--token", help="GitHub token (else GITHUB_TOKEN)")
    sp.add_argument(
        "--dry-run", action="store_true", help="print the GitHub calls instead of sending"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tracely", description="Tracely CI/CD gate")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gate", help="gate a PR on pre-emitted ci traces (matched by input)")
    g.add_argument("agent", nargs="?", help="agent slug (or --agent / TRACELY_AGENT)")
    g.add_argument("--agent", dest="agent_opt", help="agent slug")
    _add_common_gate_flags(g)

    r = sub.add_parser("replay", help="re-run the agent on each promoted case, then gate the PR")
    r.add_argument("agent", nargs="?", help="agent slug (or --agent / TRACELY_AGENT)")
    r.add_argument("--agent", dest="agent_opt", help="agent slug")
    r.add_argument(
        "--entrypoint", help="Python agent as 'module:function'; called with each case input"
    )
    r.add_argument(
        "--cmd", help="shell command to run per case (gets TRACELY_INPUT); emits its own trace"
    )
    r.add_argument(
        "--live",
        action="store_true",
        help="make real tool/LLM calls instead of serving recorded fixtures",
    )
    _add_common_gate_flags(r)

    s = sub.add_parser(
        "simulate", help="drive agents' scenarios against their endpoints, then gate the PR"
    )
    s.add_argument("agent", nargs="?", help="agent slug (or --agent / TRACELY_AGENT / --all)")
    # `agents`, not `agent_opt`: this one is a list, and the shared normalisation below must not
    # collapse it back to a single slug the way it does for gate/replay.
    s.add_argument(
        "--agent",
        dest="agents",
        action="append",
        metavar="SLUG",
        help="agent to gate; repeat the flag or comma-separate for a subset",
    )
    s.add_argument(
        "--all",
        dest="all_agents",
        action="store_true",
        help="gate every agent that has at least one enabled scenario",
    )
    s.add_argument(
        "--min-pass-rate",
        type=float,
        help="fraction of conversations that must PASS (default: the server's setting, 1.0). "
        "Lower it for adversarial suites, which land some probes by design.",
    )
    s.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="seconds to wait for the run (default 900). Timing out exits non-zero, never green.",
    )
    _add_common_gate_flags(s)

    e = sub.add_parser("export", help="dump the workspace's conversations as NDJSON")
    e.add_argument("--out", help="file to write (default: stdout)")
    e.add_argument("--api", help="Tracely API base (TRACELY_API)")
    e.add_argument("--key", help="Tracely ingest key (TRACELY_KEY)")
    e.add_argument("--limit", type=int, default=0, help="max conversations (default: all)")
    e.add_argument("--from-ts", dest="from_ts", help="ISO-8601 UTC lower bound on trace start")
    e.add_argument("--to-ts", dest="to_ts", help="ISO-8601 UTC upper bound on trace start")
    e.add_argument("--evals", action="store_true", help="also dump Tracely's own internal runs")
    e.add_argument("--meta", metavar="KEY=VALUE", help="only conversations with this metadata pair")
    # `_conn` reads all four connection fields; export has no use for the last two.
    e.set_defaults(web_url=None, agent=None)

    args = p.parse_args(argv)
    args.agent = getattr(args, "agent_opt", None) or args.agent  # allow positional or --agent
    if args.command == "gate":
        return cmd_gate(args)
    if args.command == "replay":
        return cmd_replay(args)
    if args.command == "simulate":
        return cmd_simulate(args)
    if args.command == "export":
        return cmd_export(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
