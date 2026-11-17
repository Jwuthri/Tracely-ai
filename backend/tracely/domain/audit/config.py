"""An audit's configuration: validated at the API, snapshotted onto every run.

One JSON document (`audits.config`) rather than a column per knob, for the same reason
`evaluators.config` is: the shape will keep growing (sandbox, modalities, search) and a run must be
able to freeze all of it at once — a run's results are only comparable to another run's if both
name the exact configuration they ran under.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tracely.domain.audit import dimensions as catalog
from tracely.domain.audit.judge import BEHAVIOUR, DEFAULT_THRESHOLD, INFO, VALIDITY
from tracely.domain.audit.tools import parse_tool_parameters, valid_tool_name

MAX_TURNS_CAP = 40  # attacker generations; Petri's default is 30
MAX_REPS = 10
MAX_PANEL = 5
MAX_SAMPLES = 5


class TargetTool(BaseModel):
    name: str
    description: str = ""
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not valid_tool_name(v):
            raise ValueError("tool name must be 1-64 characters of letters, digits, _ or -")
        return v

    @field_validator("parameters", mode="before")
    @classmethod
    def _params(cls, v):
        return parse_tool_parameters(v)


class Target(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    # "auditor": the attacker writes the system prompt and invents tools per seed (Petri's mode,
    # for auditing a raw model). "fixed": the prompt and tools below are the thing under test.
    control: Literal["auditor", "fixed"] = "auditor"
    system_prompt: str = ""
    tools: list[TargetTool] = Field(default_factory=list)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    reasoning_effort: Literal["", "minimal", "low", "medium", "high"] = ""


class Attacker(BaseModel):
    # Rotated by rollout index. Empty = the server's attacker model, then the default text model.
    models: list[str] = Field(default_factory=list, max_length=MAX_PANEL)
    max_turns: int = Field(default=20, ge=1, le=MAX_TURNS_CAP)
    prefill: bool = False
    rollback: bool = True
    # A cheap model vets each message / system prompt / fabricated tool result before the
    # target sees it (domain/audit/realism.py). Blank model = the default text model.
    realism_filter: bool = False
    realism_model: str = ""
    # Explore runs only (domain/audit/explore.py): draft this many moves per attacker turn and
    # play the one a picker prefers. Costs that many attacker calls a turn; 1 = off.
    best_of: int = Field(default=1, ge=1, le=4)
    # Modal tools the attacker gets (domain/audit/media.py): "image" = `generate_image`,
    # "voice" = `send_voice_message`. The target must accept the modality — checked at run start.
    media: list[Literal["image", "voice"]] = Field(default_factory=list, max_length=2)


class Environment(BaseModel):
    # "synthetic": the attacker fabricates every tool result (Petri). "sandbox": the target gets
    # a real machine and the pack's tools really execute there.
    mode: Literal["synthetic", "sandbox"] = "synthetic"
    packs: list[Literal["dev", "office", "support", "data", "devops"]] = Field(
        default_factory=lambda: ["dev"], max_length=5
    )
    # "none": the machine has no network at all. "allowlist": outbound HTTP(S) to the
    # deployment's allowlisted hosts through its egress proxy (package registries, docs).
    network: Literal["none", "allowlist"] = "none"


class Dimension(BaseModel):
    name: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")
    kind: Literal["behaviour", "validity", "info"] = BEHAVIOUR
    description: str = ""
    rubric: str = ""
    threshold: int = Field(default=DEFAULT_THRESHOLD, ge=2, le=10)
    # This dimension's own panel; empty = the audit's `judge_models`.
    models: list[str] = Field(default_factory=list, max_length=MAX_PANEL)
    samples: int = Field(default=1, ge=1, le=MAX_SAMPLES)


class Limits(BaseModel):
    budget_usd: float = Field(default=5.0, gt=0, le=1000)
    reps: int = Field(default=1, ge=1, le=MAX_REPS)


class AuditConfig(BaseModel):
    target: Target
    attacker: Attacker = Field(default_factory=Attacker)
    environment: Environment = Field(default_factory=Environment)
    dimensions: list[Dimension] = Field(default_factory=list, max_length=45)
    judge_models: list[str] = Field(default_factory=list, max_length=MAX_PANEL)
    limits: Limits = Field(default_factory=Limits)
    # Derived once by seed generation (`domain/audit/seeds.py`) and kept: `{understanding,
    # motivation}` — the shared reading of the behaviour that seeds, attacker and judges use.
    understanding: dict = Field(default_factory=dict)


def normalize(raw: dict) -> dict:
    """Validate `raw` and return the stored form. Raises `ValueError` with a readable message.

    A dimension given only by name is filled in from the built-in catalog, so the stored config
    carries the full rubric: editing the catalog later must not silently change what an existing
    audit — and every past run compared against it — is judged on."""
    raw = dict(raw or {})
    filled = []
    for dim in raw.get("dimensions") or []:
        dim = {"name": dim} if isinstance(dim, str) else dict(dim)
        base = catalog.builtin(str(dim.get("name") or "")) or {}
        filled.append({**base, **{k: v for k, v in dim.items() if v not in (None, "")}})
    raw["dimensions"] = filled
    try:
        cfg = AuditConfig.model_validate(raw)
    except Exception as exc:  # pydantic's ValidationError is a ValueError, but be explicit
        raise ValueError(_readable(exc)) from None
    names = [d.name for d in cfg.dimensions]
    if len(names) != len(set(names)):
        raise ValueError("dimension names must be unique")
    if cfg.target.control == "fixed" and not cfg.target.system_prompt.strip() and not cfg.target.tools:
        raise ValueError("a fixed target needs a system prompt or tools — otherwise there is nothing fixed to audit")
    return cfg.model_dump()


def _readable(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    return "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}" for e in errors()
    )[:600]


def resolved_dimensions(config: dict) -> list[dict]:
    """The dimensions a run judges: the audit's own (or, with none configured, the single
    "does it show this behaviour" dimension), plus the validity dimensions — always, because a
    rollout whose target noticed the test must not count for or against anything.

    An audit may override a validity dimension (its threshold, its panel) by listing it by name."""
    own = [dict(d) for d in config.get("dimensions") or []]
    if not any(d.get("kind") == BEHAVIOUR for d in own):
        own.insert(0, catalog.custom_behaviour_dimension())
    listed = {d["name"] for d in own}
    return own + [dict(d) for d in catalog.VALIDITY_DIMENSIONS if d["name"] not in listed]


__all__ = [
    "AuditConfig", "BEHAVIOUR", "INFO", "VALIDITY", "MAX_REPS", "MAX_TURNS_CAP",
    "normalize", "resolved_dimensions",
]
