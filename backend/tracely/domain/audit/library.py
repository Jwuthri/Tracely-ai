"""Petri's seed instructions and judging dimensions, vendored as data (`data/petri_*.json`).

173 seeds and 38 dimensions from github.com/meridianlabs-ai/inspect_petri (MIT — see
`data/LICENSE-petri.txt`). They are alignment-research material: most seeds assume the attacker
writes the system prompt and invents the tools, so they suit auditing a model rather than a
deployed agent with a fixed prompt.

The files carry Petri's canary string. Keep it: it is how benchmark text is kept out of
training corpora, and stripping it would undo that for everyone downstream of this repo.
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return json.loads((_DATA / name).read_text(encoding="utf-8"))


def petri_seeds(tag: str | None = None) -> list[dict]:
    """`[{id, tags, instruction}]`, optionally only those carrying `tag`."""
    seeds = _load("petri_seeds.json")["seeds"]
    return [s for s in seeds if tag in s["tags"]] if tag else seeds


def petri_tags() -> list[dict]:
    counts = Counter(t for s in petri_seeds() for t in s["tags"])
    return [{"tag": t, "count": n} for t, n in counts.most_common()]


def _kind(dim: dict) -> str:
    """Petri scores everything 1-10 with high = present, which is our scale too. What differs is
    what a high score MEANS: for most it is the finding; for the positive and scene-setting ones
    (admirable, high_stakes_setting, scenario_realism…) it is only context, so they are reported
    and never flag a rollout. Our own three validity dimensions do the excluding."""
    if dim["palette"] != "good-low" or {"eval-quality", "awareness", "positive"} & set(dim["tags"]):
        return "info"
    return "behaviour"


def petri_dimensions() -> list[dict]:
    """Petri's dimensions in the catalog's shape."""
    return [
        {"name": d["name"], "kind": _kind(d), "description": d["description"], "rubric": d["rubric"],
         "tags": d["tags"], "source": "petri"}
        for d in _load("petri_dimensions.json")["dimensions"]
    ]
