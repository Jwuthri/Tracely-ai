"""Original-vs-candidate comparison (W5): the relevant steps of two runs, aligned, with the
first meaningful divergence named — instead of two unaligned JSON blobs. Pure."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any


def _canon(v: Any) -> Any:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return v
    try:
        return json.loads(json.dumps(v, sort_keys=True, default=str))
    except (ValueError, TypeError):
        return str(v)


def relevant_steps(steps: list[dict]) -> list[dict]:
    """The steps a developer compares: tool calls and errored steps of any kind. Model calls
    are left out unless they errored — their text is the answer, compared separately."""
    tools, errored_others = [], []
    for s in steps:
        kind = str(s.get("kind") or "")
        errored = s.get("status") == "error" or s.get("level") == "ERROR"
        row = {
            "kind": kind,
            "name": str(s.get("name") or ""),
            "input": s.get("input"),
            "output": s.get("output"),
            "error": errored,
        }
        if kind == "tool":
            tools.append(row)
        elif errored:
            errored_others.append(row)
    # Errored non-tool steps (typically the agent root) go LAST: they are the run's outcome,
    # and listing them first would make every comparison open with "the root differs".
    return tools + errored_others


def align_steps(reference: list[dict], candidate: list[dict]) -> dict:
    """Align two step lists by name sequence (difflib), then compare args/error per aligned
    pair. Rows: `{ref, cand, op, divergence}` where `op` is equal | replace | delete | insert
    and `divergence` names what differs (None when nothing relevant does). `first_divergence` is
    the index of the first row that diverges, or None."""
    ref, cand = relevant_steps(reference), relevant_steps(candidate)
    sm = SequenceMatcher(a=[s["name"] for s in ref], b=[s["name"] for s in cand], autojunk=False)
    rows: list[dict] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for r, c in zip(ref[i1:i2], cand[j1:j2]):
                d = None
                if r["error"] != c["error"]:
                    d = "candidate errored" if c["error"] else "candidate no longer errors"
                elif _canon(r["input"]) != _canon(c["input"]):
                    d = "arguments differ"
                elif _canon(r["output"]) != _canon(c["output"]):
                    d = "result differs"
                rows.append({"ref": r, "cand": c, "op": "equal", "divergence": d})
        elif op == "replace":
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                r = ref[i1 + k] if i1 + k < i2 else None
                c = cand[j1 + k] if j1 + k < j2 else None
                rows.append({"ref": r, "cand": c, "op": "replace", "divergence": _replaced(r, c)})
        elif op == "delete":
            rows += [{"ref": r, "cand": None, "op": "delete", "divergence": f"{r['name']} not called by candidate"} for r in ref[i1:i2]]
        else:  # insert
            rows += [{"ref": None, "cand": c, "op": "insert", "divergence": f"candidate added {c['name']}"} for c in cand[j1:j2]]
    first = next((i for i, row in enumerate(rows) if row["divergence"]), None)
    return {"rows": rows, "first_divergence": first, "reference_steps": len(ref), "candidate_steps": len(cand)}


def _replaced(r: dict | None, c: dict | None) -> str:
    if r and c:
        return f"{r['name']} → {c['name']}"
    if r:
        return f"{r['name']} not called by candidate"
    return f"candidate added {c['name']}" if c else ""
