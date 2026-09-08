"""The durable, versioned regression-case artifact (W3): everything a case needs to execute
after its source trace has been retained away.

One JSON object per (case, case version), persisted in the blob store under
`{prefix}cases/{project_id}/{case_id}/v{version}.json`, with a content digest recorded on the
case row. Pure: build / encode / decode / digest — no I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

FORMAT_VERSION = 1
# The one execution mode a stored artifact supports today: the recorded tool/LLM bundle.
SUPPORTED_MODE = "recorded"


@dataclass(frozen=True, slots=True)
class CaseArtifact:
    case_id: str
    case_version: int
    input: dict  # {"type": "text", "value": "..."} — the typed executable input
    fixtures: dict  # FixtureBundle wire dict (v1/v2)
    expectations: dict  # {"assertions": {...}, "match_mode": "..."}
    evaluators: dict  # {score_name: judge identity} for the judges the case expects
    provenance: dict  # source trace / span / agent / version / input digest / captured_at
    # Initial state the run depends on beyond its input. External state (a DB row, a ticket, a
    # session) is NOT captured; the artifact says so rather than pretending to be self-contained.
    initial_state: dict = field(
        default_factory=lambda: {
            "captured": False,
            "note": "external state is not captured; the recorded fixtures stand in for it",
        }
    )
    execution: dict = field(default_factory=lambda: {"mode": SUPPORTED_MODE})
    format_version: int = FORMAT_VERSION

    @classmethod
    def build(
        cls,
        *,
        case_id: str,
        case_version: int,
        input_text: str,
        fixtures: dict,
        assertions: dict,
        match_mode: str,
        evaluators: dict,
        provenance: dict,
    ) -> "CaseArtifact":
        if not input_text:
            # Never invent an empty input: a case without one is not executable, full stop.
            raise ValueError("cannot snapshot a case with no executable input")
        return cls(
            case_id=case_id,
            case_version=case_version,
            input={"type": "text", "value": input_text},
            fixtures=fixtures,
            expectations={"assertions": assertions, "match_mode": match_mode},
            evaluators=evaluators,
            provenance={
                **provenance,
                "captured_at": provenance.get("captured_at")
                or datetime.now(timezone.utc).isoformat(),
            },
        )

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "case_id": self.case_id,
            "case_version": self.case_version,
            "input": self.input,
            "initial_state": self.initial_state,
            "fixtures": self.fixtures,
            "expectations": self.expectations,
            "evaluators": self.evaluators,
            "execution": self.execution,
            "provenance": self.provenance,
        }

    def encode(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, default=str).encode()

    def digest(self) -> str:
        """sha256 of the canonical encoding — the content identity a gate result pins."""
        return hashlib.sha256(self.encode()).hexdigest()

    @property
    def input_text(self) -> str:
        return str(self.input.get("value") or "")

    @classmethod
    def decode(cls, raw: bytes) -> "CaseArtifact":
        """Strict: an empty, unparsable or wrong-shaped blob raises `ValueError` so the caller
        reports an explicit incomplete state instead of a silently empty case."""
        if not raw:
            raise ValueError("case artifact is empty")
        try:
            d: Any = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"case artifact is not valid JSON: {e}") from e
        if not isinstance(d, dict) or "input" not in d or "fixtures" not in d:
            raise ValueError("case artifact has the wrong shape")
        if int(d.get("format_version", 0)) > FORMAT_VERSION:
            raise ValueError(
                f"case artifact format {d.get('format_version')} is newer than this server ({FORMAT_VERSION})"
            )
        return cls(
            case_id=str(d.get("case_id", "")),
            case_version=int(d.get("case_version", 1)),
            input=d["input"] if isinstance(d["input"], dict) else {"type": "text", "value": str(d["input"])},
            fixtures=d["fixtures"] if isinstance(d["fixtures"], dict) else {},
            expectations=d.get("expectations") or {},
            evaluators=d.get("evaluators") or {},
            provenance=d.get("provenance") or {},
            initial_state=d.get("initial_state") or {},
            execution=d.get("execution") or {"mode": SUPPORTED_MODE},
            format_version=int(d.get("format_version", FORMAT_VERSION)),
        )


def artifact_key(prefix: str, project_id: str, case_id: str, version: int) -> str:
    return f"{prefix}cases/{project_id}/{case_id}/v{version}.json"


def case_blob_prefix(prefix: str, project_id: str, case_id: str) -> str:
    """Every artifact version of one case — what deleting the case removes."""
    return f"{prefix}cases/{project_id}/{case_id}/"
