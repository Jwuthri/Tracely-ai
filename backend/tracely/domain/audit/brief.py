"""Audits as an agent reads and writes them — the in-app assistant and the MCP server.

Both reach audits through the routers, like every other tool. What they need on top is small and
pure, so it lives here once: a request body built from flat arguments (an assistant tool cannot
take a nested `config` — see `services/assistant_tools.py`), and short versions of the two reads
that are far too large to hand a model whole (a run is every judge's every answer; a rollout is
hundreds of KB of transcript).
"""

from __future__ import annotations

from typing import Any

from tracely.domain.audit import media

_TEXT = 700  # characters of one message kept in a rollout brief


def create_body(
    *,
    behaviour: str = "",
    target_model: str = "",
    name: str = "",
    preset: dict | None = None,
    imported: dict | None = None,
    agent: str = "",
    real_machine: bool = False,
    packs: list[str] | None = None,
    attacker_media: list[str] | None = None,
    judge_models: list[str] | None = None,
    budget_usd: float = 0.0,
) -> dict[str, Any]:
    """The body for `POST /api/audits`.

    `preset` is an entry of the catalog's `presets` (it supplies the behaviour, its dimensions and
    starter seeds); `imported` is what `POST /api/audits/import-target` returned for `agent` (its
    real system prompt and tools, which then become the thing under test)."""
    preset, imported = preset or {}, imported or {}
    behaviour = (behaviour or preset.get("behaviour") or "").strip()
    fixed = bool(imported.get("found"))
    config: dict[str, Any] = {
        "target": {
            "model": target_model or imported.get("model") or "",
            "control": "fixed" if fixed else "auditor",
            "system_prompt": imported.get("system_prompt") or "" if fixed else "",
            "tools": imported.get("tools") or [] if fixed else [],
        },
        "attacker": {"media": list(attacker_media or [])},
        "environment": (
            {"mode": "sandbox", "packs": list(packs or ["dev"])} if real_machine else {"mode": "synthetic"}
        ),
        "dimensions": list(preset.get("dimensions") or []),
        "judge_models": list(judge_models or []),
    }
    if budget_usd:
        config["limits"] = {"budget_usd": float(budget_usd)}
    return {
        "name": (name or preset.get("name") or " ".join(behaviour.split()[:6])).strip(),
        "behavior": behaviour,
        "agent": agent if fixed else "",
        "preset": preset.get("key"),
        "seeds": list(preset.get("seeds") or []),
        "config": config,
    }


def catalog_brief(catalog: dict) -> dict:
    """What an agent needs to choose from: presets, library topics, variation axes, packs."""
    return {
        "presets": [{"key": p["key"], "name": p["name"], "behaviour": p["behaviour"]} for p in catalog.get("presets") or []],
        "library_topics": (catalog.get("library") or {}).get("petri", {}).get("tags") or [],
        "variation_axes": catalog.get("variations") or [],
        "extra_dimensions": [d["name"] for d in catalog.get("behaviours") or []],
        "real_machine": catalog.get("sandbox") or {"available": False},
        "packs": {k: [t["name"] for t in v] for k, v in (catalog.get("packs") or {}).items()},
    }


def audit_brief(audit: dict) -> dict:
    """One audit without its dimension rubrics: what it tests, its seeds, its runs."""
    cfg = audit.get("config") or {}
    return {
        "id": audit.get("id"), "name": audit.get("name"), "behaviour": audit.get("behavior"),
        "target": {k: (cfg.get("target") or {}).get(k) for k in ("model", "control")},
        "environment": cfg.get("environment"), "budget_usd": (cfg.get("limits") or {}).get("budget_usd"),
        "seeds": [
            {"id": s["id"], "title": s.get("title"), "enabled": s.get("enabled"), "origin": s.get("origin"),
             "instruction": (s.get("instruction") or "")[:300], "files": [a["name"] for a in s.get("attachments") or []]}
            for s in audit.get("seeds") or []
        ] if isinstance(audit.get("seeds"), list) else audit.get("seeds"),
        "runs": [run_brief(r) for r in audit.get("runs") or []],
        "link": f"/audits/{audit.get('id')}",
    }


def run_brief(run: dict) -> dict:
    """A run as its headline and one line per rollout — no judge-by-judge detail."""
    summary = run.get("summary") or {}
    out = {
        "id": run.get("id"), "status": run.get("status"), "mode": run.get("mode"),
        "elicitation_rate": summary.get("elicitation_rate"), "counted": summary.get("counted"),
        "verdicts": summary.get("verdicts"), "done": run.get("done"), "total": run.get("total"),
        "spend_usd": run.get("spend_usd"), "budget_usd": run.get("budget_usd"),
        "link": f"/audits/{run.get('audit_id')}/runs/{run.get('id')}",
    }
    if run.get("mode") == "explore":
        out["note"] = "Explore run: rollouts learn from each other, so this rate is not the audit's result."
    if "rollouts" in run:
        out["rollouts"] = [
            {
                "id": r["id"], "seed": r.get("title"), "verdict": r.get("verdict") or r.get("phase"),
                "second_pass": bool(r.get("stage")),
                "scores": {x["name"]: x.get("value") for x in r.get("results") or []},
                "why": (r.get("detail") or {}).get("error") or (r.get("detail") or {}).get("reason") or "",
            }
            for r in run["rollouts"]
        ]
    return out


def rollout_brief(rollout: dict) -> dict:
    """One rollout: the verdict, each dimension's score with a judge's reason, and the
    conversation the target ended with (rolled-back branches are counted, not printed)."""
    transcript = rollout.get("transcript") or {}
    nodes = {n["id"]: n for n in transcript.get("nodes") or []}
    final: list[dict] = []
    cur = transcript.get("head") or ""
    while cur in nodes:
        final.append(nodes[cur])
        cur = nodes[cur].get("parent") or ""
    conversation = []
    for node in reversed(final):
        text = media.node_text(node)
        calls = [f"{c['name']}({str(c.get('args'))[:120]})" for c in node.get("tool_calls") or []]
        conversation.append({
            "id": node["id"], "role": node["role"],
            "text": text if len(text) <= _TEXT else text[:_TEXT] + f"… [{len(text)} chars]",
            **({"calls": calls} if calls else {}),
        })
    return {
        "id": rollout.get("id"), "seed": (rollout.get("seed") or {}).get("title") or rollout.get("title"),
        "verdict": rollout.get("verdict") or rollout.get("phase"),
        "why": (rollout.get("detail") or {}).get("error") or (rollout.get("detail") or {}).get("reason") or "",
        "judges": [
            {
                "dimension": r["name"], "kind": r.get("kind"), "score": r.get("value"),
                "flagged": r.get("flagged"), "disputed": r.get("disputed"),
                "reason": next((j.get("justification") for j in r.get("judges") or [] if j.get("justification")), ""),
            }
            for r in rollout.get("results") or []
        ],
        "rolled_back_branches": max(len(transcript.get("branches") or []) - 1, 0),
        "conversation": conversation,
    }
