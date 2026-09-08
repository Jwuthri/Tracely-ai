"""Hermetic replay fixture bundle: the recorded tool/LLM calls captured at promote time.

An ORDERED list per kind so repeated calls and per-call errors replay faithfully. Each entry
keeps the call args (to match a specific call) and the error status (so an errored production
call replays as an errored span).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FixtureCall:
    name: str  # tool name OR model id (for LLM calls)
    args: Any  # tool input OR LLM input
    output: Any  # tool result OR LLM completion
    error: str | None = None  # the span's status_message if it errored, else None
    tool_call_id: str = ""  # only set on tool fixtures (matches assistant.tool_calls[].id)


@dataclass(frozen=True, slots=True)
class FixtureBundle:
    """v2 bundle: ordered tool + LLM calls. Persisted as JSON in the blob store at
    `{s3_event_prefix}fixtures/{project_id}/{input_digest}.json`."""

    tools: list[FixtureCall] = field(default_factory=list)
    llm: list[FixtureCall] = field(default_factory=list)
    version: int = 2

    @classmethod
    def capture(cls, spans: list[dict]) -> "FixtureBundle":
        """Walk the trace's spans and snapshot every TOOL + GENERATION call in order."""
        tools: list[FixtureCall] = []
        llm: list[FixtureCall] = []
        for s in spans:
            err = s.get("status_message") if s.get("level") == "ERROR" else None
            if s.get("type") == "TOOL":
                tools.append(FixtureCall(
                    name=s.get("name") or "",
                    args=s.get("input"),
                    output=s.get("output"),
                    error=err,
                    tool_call_id=s.get("tool_call_id") or "",
                ))
            elif s.get("type") == "GENERATION":
                llm.append(FixtureCall(
                    name=s.get("name") or "",
                    args=s.get("input"),
                    output=s.get("output"),
                    error=err,
                ))
        return cls(tools=tools, llm=llm)

    def to_dict(self) -> dict:
        """Wire shape — matches the historical JSON exactly so existing fixture files keep
        round-tripping."""
        return {
            "version": self.version,
            "tools": [
                {
                    "name": t.name,
                    "args": t.args,
                    "tool_call_id": t.tool_call_id,
                    "output": t.output,
                    "error": t.error,
                }
                for t in self.tools
            ],
            "llm": [
                {"model": c.name, "input": c.args, "output": c.output, "error": c.error}
                for c in self.llm
            ],
        }

    def encode(self) -> bytes:
        """JSON bytes ready for `BlobStore.put_blob`."""
        return json.dumps(self.to_dict(), default=str).encode()

    @classmethod
    def decode(cls, raw: bytes) -> dict:
        """Decode a stored bundle. Returns the dict (not a FixtureBundle) for callers that just
        feed it back to the agent runner — they want the raw shape. An empty or unparsable blob
        raises `ValueError`: a recording that cannot be read must surface as an execution
        problem, never as "no calls recorded" (which strict replay would treat as an explicitly
        empty recording) or as live execution."""
        if not raw:
            raise ValueError("fixture bundle is empty")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"fixture bundle is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("fixture bundle is not a JSON object")
        return data
